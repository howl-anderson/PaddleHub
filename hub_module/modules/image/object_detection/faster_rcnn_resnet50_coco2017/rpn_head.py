# coding=utf-8
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from paddle import fluid
from paddle.fluid.param_attr import ParamAttr
from paddle.fluid.initializer import Normal
from paddle.fluid.regularizer import L2Decay

__all__ = ['AnchorGenerator', 'RPNTargetAssign', 'GenerateProposals', 'RPNHead']


class AnchorGenerator(object):
    # __op__ = fluid.layers.anchor_generator
    def __init__(self,
                 stride=[16.0, 16.0],
                 anchor_sizes=[32, 64, 128, 256, 512],
                 aspect_ratios=[0.5, 1., 2.],
                 variance=[1., 1., 1., 1.]):
        super(AnchorGenerator, self).__init__()
        self.anchor_sizes = anchor_sizes
        self.aspect_ratios = aspect_ratios
        self.variance = variance
        self.stride = stride


class RPNTargetAssign(object):
    # __op__ = fluid.layers.rpn_target_assign
    def __init__(self,
                 rpn_batch_size_per_im=256,
                 rpn_straddle_thresh=0.,
                 rpn_fg_fraction=0.5,
                 rpn_positive_overlap=0.7,
                 rpn_negative_overlap=0.3,
                 use_random=True):
        super(RPNTargetAssign, self).__init__()
        self.rpn_batch_size_per_im = rpn_batch_size_per_im
        self.rpn_straddle_thresh = rpn_straddle_thresh
        self.rpn_fg_fraction = rpn_fg_fraction
        self.rpn_positive_overlap = rpn_positive_overlap
        self.rpn_negative_overlap = rpn_negative_overlap
        self.use_random = use_random


class GenerateProposals(object):
    # __op__ = fluid.layers.generate_proposals
    def __init__(self,
                 pre_nms_top_n=6000,
                 post_nms_top_n=1000,
                 nms_thresh=.5,
                 min_size=.1,
                 eta=1.):
        super(GenerateProposals, self).__init__()
        self.pre_nms_top_n = pre_nms_top_n
        self.post_nms_top_n = post_nms_top_n
        self.nms_thresh = nms_thresh
        self.min_size = min_size
        self.eta = eta


class RPNHead(object):
    """
    RPN Head

    Args:
        anchor_generator (object): `AnchorGenerator` instance
        rpn_target_assign (object): `RPNTargetAssign` instance
        train_proposal (object): `GenerateProposals` instance for training
        test_proposal (object): `GenerateProposals` instance for testing
        num_classes (int): number of classes in rpn output
    """
    __inject__ = [
        'anchor_generator', 'rpn_target_assign', 'train_proposal',
        'test_proposal'
    ]

    def __init__(self,
                 anchor_generator,
                 rpn_target_assign,
                 train_proposal,
                 test_proposal,
                 num_classes=1):
        super(RPNHead, self).__init__()
        self.anchor_generator = anchor_generator
        self.rpn_target_assign = rpn_target_assign
        self.train_proposal = train_proposal
        self.test_proposal = test_proposal
        self.num_classes = num_classes

    def _get_output(self, input):
        """
        Get anchor and RPN head output.

        Args:
            input(Variable): feature map from backbone with shape of [N, C, H, W]

        Returns:
            rpn_cls_score(Variable): Output of rpn head with shape of [N, num_anchors, H, W].
            rpn_bbox_pred(Variable): Output of rpn head with shape of [N, num_anchors * 4, H, W].
        """
        dim_out = input.shape[1]
        rpn_conv = fluid.layers.conv2d(
            input=input,
            num_filters=dim_out,
            filter_size=3,
            stride=1,
            padding=1,
            act='relu',
            name='conv_rpn',
            param_attr=ParamAttr(
                name="conv_rpn_w", initializer=Normal(loc=0., scale=0.01)),
            bias_attr=ParamAttr(
                name="conv_rpn_b", learning_rate=2., regularizer=L2Decay(0.)))
        # Generate anchors self.anchor_generator
        self.anchor, self.anchor_var = fluid.layers.anchor_generator(
            input=rpn_conv,
            anchor_sizes=self.anchor_generator.anchor_sizes,
            aspect_ratios=self.anchor_generator.aspect_ratios,
            variance=self.anchor_generator.variance,
            stride=self.anchor_generator.stride)

        num_anchor = self.anchor.shape[2]
        # Proposal classification scores
        self.rpn_cls_score = fluid.layers.conv2d(
            rpn_conv,
            num_filters=num_anchor * self.num_classes,
            filter_size=1,
            stride=1,
            padding=0,
            act=None,
            name='rpn_cls_score',
            param_attr=ParamAttr(
                name="rpn_cls_logits_w", initializer=Normal(loc=0.,
                                                            scale=0.01)),
            bias_attr=ParamAttr(
                name="rpn_cls_logits_b",
                learning_rate=2.,
                regularizer=L2Decay(0.)))
        # Proposal bbox regression deltas
        self.rpn_bbox_pred = fluid.layers.conv2d(
            rpn_conv,
            num_filters=4 * num_anchor,
            filter_size=1,
            stride=1,
            padding=0,
            act=None,
            name='rpn_bbox_pred',
            param_attr=ParamAttr(
                name="rpn_bbox_pred_w", initializer=Normal(loc=0., scale=0.01)),
            bias_attr=ParamAttr(
                name="rpn_bbox_pred_b",
                learning_rate=2.,
                regularizer=L2Decay(0.)))
        return self.rpn_cls_score, self.rpn_bbox_pred

    def get_proposals(self, body_feats, im_info, mode='train'):
        """
        Get proposals according to the output of backbone.

        Args:
            body_feats (dict): The dictionary of feature maps from backbone.
            im_info(Variable): The information of image with shape [N, 3] with
                shape (height, width, scale).
            body_feat_names(list): A list of names of feature maps from
                backbone.

        Returns:
            rpn_rois(Variable): Output proposals with shape of (rois_num, 4).
        """
        # In RPN Heads, only the last feature map of backbone is used.
        # And body_feat_names[-1] represents the last level name of backbone.
        body_feat = list(body_feats.values())[-1]
        rpn_cls_score, rpn_bbox_pred = self._get_output(body_feat)

        if self.num_classes == 1:
            rpn_cls_prob = fluid.layers.sigmoid(
                rpn_cls_score, name='rpn_cls_prob')
        else:
            rpn_cls_score = fluid.layers.transpose(
                rpn_cls_score, perm=[0, 2, 3, 1])
            rpn_cls_score = fluid.layers.reshape(
                rpn_cls_score, shape=(0, 0, 0, -1, self.num_classes))
            rpn_cls_prob_tmp = fluid.layers.softmax(
                rpn_cls_score, use_cudnn=False, name='rpn_cls_prob')
            rpn_cls_prob_slice = fluid.layers.slice(
                rpn_cls_prob_tmp, axes=[4], starts=[1], ends=[self.num_classes])
            rpn_cls_prob, _ = fluid.layers.topk(rpn_cls_prob_slice, 1)
            rpn_cls_prob = fluid.layers.reshape(
                rpn_cls_prob, shape=(0, 0, 0, -1))
            rpn_cls_prob = fluid.layers.transpose(
                rpn_cls_prob, perm=[0, 3, 1, 2])
        prop_op = self.train_proposal if mode == 'train' else self.test_proposal
        # prop_op
        rpn_rois, rpn_roi_probs = fluid.layers.generate_proposals(
            scores=rpn_cls_prob,
            bbox_deltas=rpn_bbox_pred,
            im_info=im_info,
            anchors=self.anchor,
            variances=self.anchor_var,
            pre_nms_top_n=prop_op.pre_nms_top_n,
            post_nms_top_n=prop_op.post_nms_top_n,
            nms_thresh=prop_op.nms_thresh,
            min_size=prop_op.min_size,
            eta=prop_op.eta)
        return rpn_rois

    def _transform_input(self, rpn_cls_score, rpn_bbox_pred, anchor,
                         anchor_var):
        rpn_cls_score = fluid.layers.transpose(rpn_cls_score, perm=[0, 2, 3, 1])
        rpn_bbox_pred = fluid.layers.transpose(rpn_bbox_pred, perm=[0, 2, 3, 1])
        anchor = fluid.layers.reshape(anchor, shape=(-1, 4))
        anchor_var = fluid.layers.reshape(anchor_var, shape=(-1, 4))
        rpn_cls_score = fluid.layers.reshape(
            x=rpn_cls_score, shape=(0, -1, self.num_classes))
        rpn_bbox_pred = fluid.layers.reshape(x=rpn_bbox_pred, shape=(0, -1, 4))
        return rpn_cls_score, rpn_bbox_pred, anchor, anchor_var

    def _get_loss_input(self):
        for attr in ['rpn_cls_score', 'rpn_bbox_pred', 'anchor', 'anchor_var']:
            if not getattr(self, attr, None):
                raise ValueError("self.{} should not be None,".format(attr),
                                 "call RPNHead.get_proposals first")
        return self._transform_input(self.rpn_cls_score, self.rpn_bbox_pred,
                                     self.anchor, self.anchor_var)

    def get_loss(self, im_info, gt_box, is_crowd, gt_label=None):
        """
        Sample proposals and Calculate rpn loss.

        Args:
            im_info(Variable): The information of image with shape [N, 3] with
                shape (height, width, scale).
            gt_box(Variable): The ground-truth bounding boxes with shape [M, 4].
                M is the number of groundtruth.
            is_crowd(Variable): Indicates groud-truth is crowd or not with
                shape [M, 1]. M is the number of groundtruth.

        Returns:
            Type: dict
                rpn_cls_loss(Variable): RPN classification loss.
                rpn_bbox_loss(Variable): RPN bounding box regression loss.

        """
        rpn_cls, rpn_bbox, anchor, anchor_var = self._get_loss_input()
        if self.num_classes == 1:
            # self.rpn_target_assign
            score_pred, loc_pred, score_tgt, loc_tgt, bbox_weight = \
                fluid.layers.rpn_target_assign(
                    bbox_pred=rpn_bbox,
                    cls_logits=rpn_cls,
                    anchor_box=anchor,
                    anchor_var=anchor_var,
                    gt_boxes=gt_box,
                    is_crowd=is_crowd,
                    im_info=im_info,
                    rpn_batch_size_per_im=self.rpn_target_assign.rpn_batch_size_per_im,
                    rpn_straddle_thresh=self.rpn_target_assign.rpn_straddle_thresh,
                    rpn_fg_fraction=self.rpn_target_assign.rpn_fg_fraction,
                    rpn_positive_overlap=self.rpn_target_assign.rpn_positive_overlap,
                    rpn_negative_overlap=self.rpn_target_assign.rpn_negative_overlap,
                    use_random=self.rpn_target_assign.use_random)
            score_tgt = fluid.layers.cast(x=score_tgt, dtype='float32')
            score_tgt.stop_gradient = True
            rpn_cls_loss = fluid.layers.sigmoid_cross_entropy_with_logits(
                x=score_pred, label=score_tgt)
        else:
            score_pred, loc_pred, score_tgt, loc_tgt, bbox_weight = \
                self.rpn_target_assign(
                    bbox_pred=rpn_bbox,
                    cls_logits=rpn_cls,
                    anchor_box=anchor,
                    anchor_var=anchor_var,
                    gt_boxes=gt_box,
                    gt_labels=gt_label,
                    is_crowd=is_crowd,
                    num_classes=self.num_classes,
                    im_info=im_info)
            labels_int64 = fluid.layers.cast(x=score_tgt, dtype='int64')
            labels_int64.stop_gradient = True
            rpn_cls_loss = fluid.layers.softmax_with_cross_entropy(
                logits=score_pred, label=labels_int64, numeric_stable_mode=True)

        rpn_cls_loss = fluid.layers.reduce_mean(
            rpn_cls_loss, name='loss_rpn_cls')

        loc_tgt = fluid.layers.cast(x=loc_tgt, dtype='float32')
        loc_tgt.stop_gradient = True
        rpn_reg_loss = fluid.layers.smooth_l1(
            x=loc_pred,
            y=loc_tgt,
            sigma=3.0,
            inside_weight=bbox_weight,
            outside_weight=bbox_weight)
        rpn_reg_loss = fluid.layers.reduce_sum(
            rpn_reg_loss, name='loss_rpn_bbox')
        score_shape = fluid.layers.shape(score_tgt)
        score_shape = fluid.layers.cast(x=score_shape, dtype='float32')
        norm = fluid.layers.reduce_prod(score_shape)
        norm.stop_gradient = True
        rpn_reg_loss = rpn_reg_loss / norm
        return {'rpn_cls_loss': rpn_cls_loss, 'rpn_reg_loss': rpn_reg_loss}
