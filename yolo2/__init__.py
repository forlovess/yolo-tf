"""
Copyright (C) 2017, 申瑞珉 (Ruimin Shen)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import configparser
import os
import numpy as np
import pandas as pd
import tensorflow as tf
import yolo
import yolo2.inference as inference


def transform_labels_voc(imageshapes, labels, width, height, cell_width, cell_height, classes):
    mask, prob, coords, offset_xy_min, offset_xy_max, areas = yolo.transform_labels_voc(imageshapes, labels, width, height, cell_width, cell_height, classes)
    prob = np.expand_dims(prob, 2)
    return mask, prob, coords, offset_xy_min, offset_xy_max, areas


class Model(object):
    def __init__(self, net, classes, anchors):
        _, self.cell_height, self.cell_width, _ = net.get_shape().as_list()
        cells = self.cell_height * self.cell_width
        output = tf.reshape(net, [-1, cells, len(anchors), 5 + classes], name='output')
        with tf.name_scope('labels'):
            output_sigmoid = tf.nn.sigmoid(output[:, :, :, :3])
            end = 1
            self.iou = output_sigmoid[:, :, :, end]
            start = end
            end += 2
            self.offset_xy = tf.identity(output_sigmoid[:, :, :, start:end], name='offset_xy')
            start = end
            end += 2
            self.wh = tf.identity(tf.exp(output[:, :, :, start:end]) * np.reshape(anchors, [1, 1, len(anchors), -1]), name='wh')
            self.areas = tf.identity(self.wh[:, :, :, 0] * self.wh[:, :, :, 1], name='areas')
            _wh = self.wh / 2
            self.offset_xy_min = tf.identity(self.offset_xy - _wh, name='offset_xy_min')
            self.offset_xy_max = tf.identity(self.offset_xy + _wh, name='offset_xy_max')
            self.wh01 = tf.identity(self.wh / np.reshape([self.cell_width, self.cell_height], [1, 1, 1, 2]), name='wh01')
            self.wh01_sqrt = tf.sqrt(self.wh01, name='wh01_sqrt')
            self.coords = tf.concat([self.offset_xy, self.wh01_sqrt], -1, name='coords')
            self.prob = tf.nn.softmax(output[:, :, :, end:])
        with tf.name_scope('detection'):
            cell_xy = yolo.calc_cell_xy(self.cell_height, self.cell_width).reshape([1, cells, 1, 2])
            self.xy = tf.identity(cell_xy + self.offset_xy, name='xy')
            self.xy_min = tf.identity(cell_xy + self.offset_xy_min, name='xy_min')
            self.xy_max = tf.identity(cell_xy + self.offset_xy_max, name='xy_max')
            self.conf = tf.identity(self.prob * tf.expand_dims(self.iou, -1), name='conf')
        self.classes = classes
        self.anchors = anchors


class Loss(dict):
    def __init__(self, model, mask, prob, coords, offset_xy_min, offset_xy_max, areas):
        self.model = model
        self.mask = mask
        self.prob = prob
        self.coords = coords
        self.offset_xy_min = offset_xy_min
        self.offset_xy_max = offset_xy_max
        self.areas = areas
        with tf.name_scope('iou'):
            _offset_xy_min = tf.maximum(model.offset_xy_min, self.offset_xy_min) 
            _offset_xy_max = tf.minimum(model.offset_xy_max, self.offset_xy_max)
            _wh = tf.maximum(_offset_xy_max - _offset_xy_min, 0.0)
            _areas = _wh[:, :, :, 0] * _wh[:, :, :, 1]
            areas = tf.maximum(self.areas + model.areas - _areas, 1e-10)
            iou = tf.truediv(_areas, areas, name='iou')
        with tf.name_scope('mask'):
            max_iou = tf.reduce_max(iou, 2, True, name='max_iou')
            mask_max_iou = tf.to_float(tf.equal(iou, max_iou, name='mask_max_iou'))
            mask_best = tf.identity(self.mask * mask_max_iou, name='mask_best')
            mask_normal = tf.identity(1 - mask_best, name='mask_normal')
        iou_diff = tf.identity(model.iou - iou, name='iou_diff')
        with tf.name_scope('objectives'):
            self['prob'] = tf.nn.l2_loss(tf.expand_dims(self.mask, -1) * model.prob - self.prob, name='prob')
            self['iou_best'] = tf.nn.l2_loss(mask_best * iou_diff, name='mask_best')
            self['iou_normal'] = tf.nn.l2_loss(mask_normal * iou_diff, name='mask_normal')
            self['coords'] = tf.nn.l2_loss(tf.expand_dims(mask_best, -1) * (model.coords - self.coords), name='coords')


class Builder(yolo.Builder):
    def __init__(self, args, config):
        section = __name__.split('.')[-1]
        self.args = args
        self.config = config
        with open(os.path.expanduser(os.path.expandvars(config.get(section, 'names'))), 'r') as f:
            self.names = [line.strip() for line in f]
        self.width = config.getint(section, 'width')
        self.height = config.getint(section, 'height')
        self.anchors = pd.read_csv(os.path.expanduser(os.path.expandvars(config.get(section, 'anchors'))), sep='\t').values
        self.inference = getattr(inference, config.get(section, 'inference'))
    
    def train(self, data, labels, scope='train'):
        section = __name__.split('.')[-1]
        _, net = self.inference(data, len(self.names), len(self.anchors), training=True)
        with tf.name_scope(scope):
            with tf.name_scope('model'):
                self.model_train = Model(net, len(self.names), self.anchors)
            with tf.name_scope('loss'):
                self.loss_train = Loss(self.model_train, *labels)
                with tf.variable_scope('hparam'):
                    self.hparam = dict([(key, tf.Variable(float(s), name='hparam_' + key, trainable=False)) for key, s in self.config.items(section + '_hparam')])
                with tf.name_scope('loss_objectives'):
                    loss_objectives = tf.reduce_sum([self.loss_train[key] * self.hparam[key] for key in self.loss_train], name='loss_objectives')
                self.loss = loss_objectives
                for key in self.loss_train:
                    tf.summary.scalar(key, self.loss_train[key])
                tf.summary.scalar('loss', self.loss)
    
    def log_hparam(self, sess, logger):
        keys, values = zip(*self.hparam.items())
        logger.info(', '.join(['%s=%f' % (key, value) for key, value in zip(keys, sess.run(values))]))
    
    def eval(self, data, scope='eval'):
        _, net = self.inference(data, len(self.names), len(self.anchors))
        with tf.name_scope(scope):
            self.model_eval = Model(net, len(self.names), self.anchors)