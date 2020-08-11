#! /usr/bin/env python
# coding=utf-8
# ================================================================
#
#   Author      : miemie2013
#   Created date: 2020-05-20 15:35:27
#   Description : keras_yolov4
#
# ================================================================
import cv2
from collections import deque
import math
import json
import time
import threading
import datetime
import random
import copy
import numpy as np
import os
import tensorflow as tf

# import keras
# import keras.layers as layers
# from keras import backend as K

from tensorflow.python import keras
from tensorflow.python.keras import layers
from tensorflow.python.keras import backend as K

from config import TrainConfig
from model.yolov4 import YOLOv4
from tools.cocotools import get_classes, catid2clsid, clsid2catid
from model.decode_np import Decode
from tools.cocotools import eval
from tools.data_process import data_clean, get_samples
from tools.transform import *
from pycocotools.coco import COCO

import logging
FORMAT = '%(asctime)s-%(levelname)s: %(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)
logger = logging.getLogger(__name__)


# 显存分配
os.environ["CUDA_VISIBLE_DEVICES"] = "0"
from keras.backend.tensorflow_backend import set_session
config = tf.ConfigProto()
config.gpu_options.per_process_gpu_memory_fraction = 1.0
set_session(tf.Session(config=config))

def bbox_ciou(boxes1, boxes2):
    '''
    计算ciou = iou - p2/c2 - av
    :param boxes1: (8, 13, 13, 3, 4)   pred_xywh
    :param boxes2: (8, 13, 13, 3, 4)   label_xywh
    :return:

    举例时假设pred_xywh和label_xywh的shape都是(1, 4)
    '''

    # 变成左上角坐标、右下角坐标
    boxes1_x0y0x1y1 = tf.concat([boxes1[..., :2] - boxes1[..., 2:] * 0.5,
                                 boxes1[..., :2] + boxes1[..., 2:] * 0.5], axis=-1)
    boxes2_x0y0x1y1 = tf.concat([boxes2[..., :2] - boxes2[..., 2:] * 0.5,
                                 boxes2[..., :2] + boxes2[..., 2:] * 0.5], axis=-1)
    '''
    逐个位置比较boxes1_x0y0x1y1[..., :2]和boxes1_x0y0x1y1[..., 2:]，即逐个位置比较[x0, y0]和[x1, y1]，小的留下。
    比如留下了[x0, y0]
    这一步是为了避免一开始w h 是负数，导致x0y0成了右下角坐标，x1y1成了左上角坐标。
    '''
    boxes1_x0y0x1y1 = tf.concat([tf.minimum(boxes1_x0y0x1y1[..., :2], boxes1_x0y0x1y1[..., 2:]),
                                 tf.maximum(boxes1_x0y0x1y1[..., :2], boxes1_x0y0x1y1[..., 2:])], axis=-1)
    boxes2_x0y0x1y1 = tf.concat([tf.minimum(boxes2_x0y0x1y1[..., :2], boxes2_x0y0x1y1[..., 2:]),
                                 tf.maximum(boxes2_x0y0x1y1[..., :2], boxes2_x0y0x1y1[..., 2:])], axis=-1)

    # 两个矩形的面积
    boxes1_area = (boxes1_x0y0x1y1[..., 2] - boxes1_x0y0x1y1[..., 0]) * (
                boxes1_x0y0x1y1[..., 3] - boxes1_x0y0x1y1[..., 1])
    boxes2_area = (boxes2_x0y0x1y1[..., 2] - boxes2_x0y0x1y1[..., 0]) * (
                boxes2_x0y0x1y1[..., 3] - boxes2_x0y0x1y1[..., 1])

    # 相交矩形的左上角坐标、右下角坐标，shape 都是 (8, 13, 13, 3, 2)
    left_up = tf.maximum(boxes1_x0y0x1y1[..., :2], boxes2_x0y0x1y1[..., :2])
    right_down = tf.minimum(boxes1_x0y0x1y1[..., 2:], boxes2_x0y0x1y1[..., 2:])

    # 相交矩形的面积inter_area。iou
    inter_section = tf.maximum(right_down - left_up, 0.0)
    inter_area = inter_section[..., 0] * inter_section[..., 1]
    union_area = boxes1_area + boxes2_area - inter_area
    iou = inter_area / (union_area + 1e-9)

    # 包围矩形的左上角坐标、右下角坐标，shape 都是 (8, 13, 13, 3, 2)
    enclose_left_up = tf.minimum(boxes1_x0y0x1y1[..., :2], boxes2_x0y0x1y1[..., :2])
    enclose_right_down = tf.maximum(boxes1_x0y0x1y1[..., 2:], boxes2_x0y0x1y1[..., 2:])

    # 包围矩形的对角线的平方
    enclose_wh = enclose_right_down - enclose_left_up
    enclose_c2 = K.pow(enclose_wh[..., 0], 2) + K.pow(enclose_wh[..., 1], 2)

    # 两矩形中心点距离的平方
    p2 = K.pow(boxes1[..., 0] - boxes2[..., 0], 2) + K.pow(boxes1[..., 1] - boxes2[..., 1], 2)

    # 增加av。加上除0保护防止nan。
    atan1 = tf.atan(boxes1[..., 2] / (boxes1[..., 3] + 1e-9))
    atan2 = tf.atan(boxes2[..., 2] / (boxes2[..., 3] + 1e-9))
    v = 4.0 * K.pow(atan1 - atan2, 2) / (math.pi ** 2)
    a = v / (1 - iou + v)

    ciou = iou - 1.0 * p2 / enclose_c2 - 1.0 * a * v
    return ciou


def bbox_iou(boxes1, boxes2):
    '''
    预测框          boxes1 (?, grid_h, grid_w, 3,   1, 4)，神经网络的输出(tx, ty, tw, th)经过了后处理求得的(bx, by, bw, bh)
    图片中所有的gt  boxes2 (?,      1,      1, 1, 70, 4)
    '''
    boxes1_area = boxes1[..., 2] * boxes1[..., 3]  # 所有格子的3个预测框的面积
    boxes2_area = boxes2[..., 2] * boxes2[..., 3]  # 所有ground truth的面积

    # (x, y, w, h)变成(x0, y0, x1, y1)
    boxes1 = tf.concat([boxes1[..., :2] - boxes1[..., 2:] * 0.5,
                        boxes1[..., :2] + boxes1[..., 2:] * 0.5], axis=-1)
    boxes2 = tf.concat([boxes2[..., :2] - boxes2[..., 2:] * 0.5,
                        boxes2[..., :2] + boxes2[..., 2:] * 0.5], axis=-1)

    # 所有格子的3个预测框 分别 和  70个ground truth  计算iou。 所以left_up和right_down的shape = (?, grid_h, grid_w, 3, 70, 2)
    left_up = tf.maximum(boxes1[..., :2], boxes2[..., :2])  # 相交矩形的左上角坐标
    right_down = tf.minimum(boxes1[..., 2:], boxes2[..., 2:])  # 相交矩形的右下角坐标

    inter_section = tf.maximum(right_down - left_up, 0.0)  # 相交矩形的w和h，是负数时取0     (?, grid_h, grid_w, 3, 70, 2)
    inter_area = inter_section[..., 0] * inter_section[..., 1]  # 相交矩形的面积            (?, grid_h, grid_w, 3, 70)
    union_area = boxes1_area + boxes2_area - inter_area  # union_area      (?, grid_h, grid_w, 3, 70)
    iou = 1.0 * inter_area / union_area  # iou                             (?, grid_h, grid_w, 3, 70)
    return iou

def loss_layer(conv, pred, label, bboxes, stride, num_class, iou_loss_thresh):
    conv_shape = tf.shape(conv)
    batch_size = conv_shape[0]
    output_size = conv_shape[1]
    input_size = stride * output_size
    conv = tf.reshape(conv, (batch_size, output_size, output_size,
                             3, 5 + num_class))
    conv_raw_prob = conv[:, :, :, :, 5:]

    pred_xywh = pred[:, :, :, :, 0:4]
    pred_conf = pred[:, :, :, :, 4:5]

    label_xywh = label[:, :, :, :, 0:4]
    respond_bbox = label[:, :, :, :, 4:5]
    label_prob = label[:, :, :, :, 5:]

    ciou = tf.expand_dims(bbox_ciou(pred_xywh, label_xywh), axis=-1)  # (8, 13, 13, 3, 1)
    input_size = tf.cast(input_size, tf.float32)

    # 每个预测框xxxiou_loss的权重 = 2 - (ground truth的面积/图片面积)
    bbox_loss_scale = 2.0 - 1.0 * label_xywh[:, :, :, :, 2:3] * label_xywh[:, :, :, :, 3:4] / (input_size ** 2)
    ciou_loss = respond_bbox * bbox_loss_scale * (1 - ciou)  # 1. respond_bbox作为mask，有物体才计算xxxiou_loss

    # 2. respond_bbox作为mask，有物体才计算类别loss
    prob_loss = respond_bbox * tf.nn.sigmoid_cross_entropy_with_logits(labels=label_prob, logits=conv_raw_prob)
    # 等价于
    # pred_prob = pred[:, :, :, :, 5:]
    # prob_pos_loss = label_prob * (0 - K.log(pred_prob + 1e-9))
    # prob_neg_loss = (1 - label_prob) * (0 - K.log(1 - pred_prob + 1e-9))
    # prob_mask = tf.tile(respond_bbox, [1, 1, 1, 1, num_class])
    # prob_loss = prob_mask * (prob_pos_loss + prob_neg_loss)


    # 3. xxxiou_loss和类别loss比较简单。重要的是conf_loss，是一个二值交叉熵损失
    # 分两步：第一步是确定 grid_h * grid_w * 3 个预测框 哪些作为反例；第二步是计算二值交叉熵损失。
    expand_pred_xywh = pred_xywh[:, :, :, :, np.newaxis, :]  # 扩展为(?, grid_h, grid_w, 3,   1, 4)
    expand_bboxes = bboxes[:, np.newaxis, np.newaxis, np.newaxis, :, :]  # 扩展为(?,      1,      1, 1, 70, 4)
    iou = bbox_iou(expand_pred_xywh, expand_bboxes)  # 所有格子的3个预测框 分别 和  70个ground truth  计算iou。   (?, grid_h, grid_w, 3, 70)
    max_iou = tf.expand_dims(tf.reduce_max(iou, axis=-1), axis=-1)  # 与70个ground truth的iou中，保留最大那个iou。  (?, grid_h, grid_w, 3, 1)

    # respond_bgd代表  这个分支输出的 grid_h * grid_w * 3 个预测框是否是 反例（背景）
    # label有物体，respond_bgd是0。 没物体的话：如果和某个gt(共70个)的iou超过iou_loss_thresh，respond_bgd是0；如果和所有gt(最多70个)的iou都小于iou_loss_thresh，respond_bgd是1。
    # respond_bgd是0代表有物体，不是反例（或者是忽略框）；  权重respond_bgd是1代表没有物体，是反例。
    # 有趣的是，模型训练时由于不断更新，对于同一张图片，两次预测的 grid_h * grid_w * 3 个预测框（对于这个分支输出）  是不同的。用的是这些预测框来与gt计算iou来确定哪些预测框是反例。
    # 而不是用固定大小（不固定位置）的先验框。
    respond_bgd = (1.0 - respond_bbox) * tf.cast(max_iou < iou_loss_thresh, tf.float32)

    # 二值交叉熵损失
    pos_loss = respond_bbox * (0 - K.log(pred_conf + 1e-9))
    neg_loss = respond_bgd  * (0 - K.log(1 - pred_conf + 1e-9))

    conf_loss = pos_loss + neg_loss
    # 回顾respond_bgd，某个预测框和某个gt的iou超过iou_loss_thresh，不被当作是反例。在参与“预测的置信位 和 真实置信位 的 二值交叉熵”时，这个框也可能不是正例(label里没标这个框是1的话)。这个框有可能不参与置信度loss的计算。
    # 这种框一般是gt框附近的框，或者是gt框所在格子的另外两个框。它既不是正例也不是反例不参与置信度loss的计算。（论文里称之为ignore）

    ciou_loss = tf.reduce_mean(tf.reduce_sum(ciou_loss, axis=[1, 2, 3, 4]))  # 每个样本单独计算自己的ciou_loss，再求平均值
    conf_loss = tf.reduce_mean(tf.reduce_sum(conf_loss, axis=[1, 2, 3, 4]))  # 每个样本单独计算自己的conf_loss，再求平均值
    prob_loss = tf.reduce_mean(tf.reduce_sum(prob_loss, axis=[1, 2, 3, 4]))  # 每个样本单独计算自己的prob_loss，再求平均值

    return ciou_loss, conf_loss, prob_loss

def decode(conv_output, anchors, stride, num_class):
    conv_shape       = tf.shape(conv_output)
    batch_size       = conv_shape[0]
    output_size      = conv_shape[1]
    anchor_per_scale = len(anchors)
    conv_output = tf.reshape(conv_output, (batch_size, output_size, output_size, anchor_per_scale, 5 + num_class))
    conv_raw_dxdy = conv_output[:, :, :, :, 0:2]
    conv_raw_dwdh = conv_output[:, :, :, :, 2:4]
    conv_raw_conf = conv_output[:, :, :, :, 4:5]
    conv_raw_prob = conv_output[:, :, :, :, 5: ]
    y = tf.tile(tf.range(output_size, dtype=tf.int32)[:, tf.newaxis], [1, output_size])
    x = tf.tile(tf.range(output_size, dtype=tf.int32)[tf.newaxis, :], [output_size, 1])
    xy_grid = tf.concat([x[:, :, tf.newaxis], y[:, :, tf.newaxis]], axis=-1)
    xy_grid = tf.tile(xy_grid[tf.newaxis, :, :, tf.newaxis, :], [batch_size, 1, 1, anchor_per_scale, 1])
    xy_grid = tf.cast(xy_grid, tf.float32)
    pred_xy = (tf.sigmoid(conv_raw_dxdy) + xy_grid) * stride
    pred_wh = (tf.exp(conv_raw_dwdh) * anchors)
    pred_xywh = tf.concat([pred_xy, pred_wh], axis=-1)
    pred_conf = tf.sigmoid(conv_raw_conf)
    pred_prob = tf.sigmoid(conv_raw_prob)
    return tf.concat([pred_xywh, pred_conf, pred_prob], axis=-1)

def yolo_loss(args, num_classes, iou_loss_thresh, anchors):
    conv_lbbox = args[0]   # (?, ?, ?, 3*(num_classes+5))
    conv_mbbox = args[1]   # (?, ?, ?, 3*(num_classes+5))
    conv_sbbox = args[2]   # (?, ?, ?, 3*(num_classes+5))
    label_sbbox = args[3]   # (?, ?, ?, 3, num_classes+5)
    label_mbbox = args[4]   # (?, ?, ?, 3, num_classes+5)
    label_lbbox = args[5]   # (?, ?, ?, 3, num_classes+5)
    true_bboxes = args[6]   # (?, 50, 4)
    pred_sbbox = decode(conv_sbbox, anchors[0], 8, num_classes)
    pred_mbbox = decode(conv_mbbox, anchors[1], 16, num_classes)
    pred_lbbox = decode(conv_lbbox, anchors[2], 32, num_classes)
    sbbox_ciou_loss, sbbox_conf_loss, sbbox_prob_loss = loss_layer(conv_sbbox, pred_sbbox, label_sbbox, true_bboxes, 8, num_classes, iou_loss_thresh)
    mbbox_ciou_loss, mbbox_conf_loss, mbbox_prob_loss = loss_layer(conv_mbbox, pred_mbbox, label_mbbox, true_bboxes, 16, num_classes, iou_loss_thresh)
    lbbox_ciou_loss, lbbox_conf_loss, lbbox_prob_loss = loss_layer(conv_lbbox, pred_lbbox, label_lbbox, true_bboxes, 32, num_classes, iou_loss_thresh)

    ciou_loss = sbbox_ciou_loss + mbbox_ciou_loss + lbbox_ciou_loss
    conf_loss = sbbox_conf_loss + mbbox_conf_loss + lbbox_conf_loss
    prob_loss = sbbox_prob_loss + mbbox_prob_loss + lbbox_prob_loss
    return [ciou_loss, conf_loss, prob_loss]


def multi_thread_op(i, samples, decodeImage, context, train_dataset, with_mixup, mixupImage,
                     photometricDistort, randomCrop, randomFlipImage, normalizeBox, padBox, bboxXYXY2XYWH):
    samples[i] = decodeImage(samples[i], context, train_dataset)
    if with_mixup:
        samples[i] = mixupImage(samples[i], context)
    samples[i] = photometricDistort(samples[i], context)
    samples[i] = randomCrop(samples[i], context)
    samples[i] = randomFlipImage(samples[i], context)
    samples[i] = normalizeBox(samples[i], context)
    samples[i] = padBox(samples[i], context)
    samples[i] = bboxXYXY2XYWH(samples[i], context)

def freeze_session(session, keep_var_names=None, output_names=None, clear_devices=True):
    from tensorflow.python.framework.graph_util import convert_variables_to_constants
    graph = session.graph
    with graph.as_default():
        freeze_var_names = list(set(v.op.name for v in tf.global_variables()).difference(keep_var_names or []))
        output_names = output_names or []
        output_names += [v.op.name for v in tf.global_variables()]
        # Graph -> GraphDef ProtoBuf
        input_graph_def = graph.as_graph_def(add_shapes=True)
        if clear_devices:
            for node in input_graph_def.node:
                node.device = ""
        frozen_graph = convert_variables_to_constants(session, input_graph_def,
                                                      output_names, freeze_var_names)
        return frozen_graph

if __name__ == '__main__':
    cfg = TrainConfig()
    tf.keras.backend.set_learning_phase(0)

    class_names = get_classes(cfg.classes_path)
    num_classes = len(class_names)
    _anchors = copy.deepcopy(cfg.anchors)
    num_anchors = len(cfg.anchor_masks[0])  # 每个输出层有几个先验框
    _anchors = np.array(_anchors)
    _anchors = np.reshape(_anchors, (-1, num_anchors, 2))
    _anchors = _anchors.astype(np.float32)

    # 步id，无需设置，会自动读。
    iter_id = 0

    # 多尺度训练
    inputs = layers.Input(shape=(None, None, 3))
    model_body = YOLOv4(inputs, num_classes, num_anchors)
    _decode = Decode(cfg.conf_thresh, cfg.nms_thresh, cfg.input_shape, model_body, class_names)

    model_body.load_weights(cfg.model_path, by_name=True) 

    y_true = [
        layers.Input(name='input_2', shape=(None, None, 3, (num_classes + 5))),  # label_sbbox
        layers.Input(name='input_3', shape=(None, None, 3, (num_classes + 5))),  # label_mbbox
        layers.Input(name='input_4', shape=(None, None, 3, (num_classes + 5))),  # label_lbbox
        layers.Input(name='input_5', shape=(cfg.num_max_boxes, 4)),             # true_bboxes
    ]
    loss_list = layers.Lambda(yolo_loss, name='yolo_loss',
                           arguments={'num_classes': num_classes, 'iou_loss_thresh': cfg.iou_loss_thresh,
                                      'anchors': _anchors})([*model_body.output, *y_true])
    model = keras.models.Model([model_body.input, *y_true], loss_list)
    model.summary()
    # keras.utils.vis_utils.plot_model(model_body, to_file='yolov4.png', show_shapes=True)

    frozen_graph = freeze_session(tf.keras.backend.get_session(), output_names=[out.op.name for out in model.outputs])
    tf.train.write_graph(frozen_graph, "pb", "tf_model.pb", as_text=False)

