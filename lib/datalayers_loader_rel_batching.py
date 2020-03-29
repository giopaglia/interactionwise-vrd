import numpy as np
import os.path as osp
import scipy.io as sio
import scipy
# import cv2
import pickle
from lib.blob import prep_im_for_blob, im_list_to_blob
from lib.dataset import dataset
import torch
import random



import utils
from copy import copy, deepcopy
from matplotlib import pyplot
from torch.utils import data


# TODO: expand so that it supports batch sizes > 1


class VRDDataLayer(data.Dataset):
    """ Iterate through the dataset and yield the input and target for the network """

    def __init__(self, ds_info, stage, shuffle=False):
        ds_info = copy(ds_info)
        if isinstance(ds_info, str):
            ds_name = ds_info
            ds_args = {}
        else:
            ds_name = ds_info["ds_name"]
            del ds_info["ds_name"]
            ds_args = ds_info

        self.ds_name = ds_name
        self.stage = stage
        # TODO: Receive this parameter as an argument, don't hardcode this
        self.granularity = "rel"

        self.dataset = dataset(self.ds_name, **ds_args)
        self.soP_prior = self.dataset.getDistribution("soP")

        self.n_obj = self.dataset.n_obj
        self.n_pred = self.dataset.n_pred

        self.shuffle = shuffle
        self.imgrels = deepcopy(self.dataset.getRelst(self.stage, self.granularity))

        # TODO: check if this works
        if self.stage == "train":
            self.imgrels = [(k, v) for k, v in self.imgrels if k != None]

        if self.shuffle:
            if self.stage != "train":
                print("WARNING! You shouldn't shuffle if not during training")
            random.shuffle(self.imgrels)

        self.n_imgrels = len(self.imgrels)
        self._cur = 0
        self.wrap_around = (self.stage == "train")

        self.batch_size = 1
        # TODO: take care of the remaining
        self.n_imgrel_batches = self.n_imgrels // self.batch_size

        self.objdet_res = False

        # self.max_shape = self._get_max_shape()
        '''
        this is the max shape that was identified by running the function above on the
        VRD dataset. It takes too long to run, so it's better if we run this once on
        any new dataset, and store and initialize those values as done here
        '''
        self.max_shape = (1000, 1000, 3)
        print("Max shape is: {}".format(self.max_shape))

        # print("Loading Word2Vec model...")
        # self.w2v_model = KeyedVectors.load_word2vec_format(osp.join(globals.data_dir, globals.w2v_model_path), binary=True)

    def _get_max_shape(self):
        print("Identifying max shape...")
        im_shapes = []
        for img in self.imgrels:
            img_id, _ = img
            if img_id is not None:
                im = utils.read_img(osp.join(self.dataset.img_dir, img_id))
                image_blob, _ = prep_im_for_blob(im, utils.vrd_pixel_means)
                im_shapes.append(image_blob.shape)

        max_shape = np.array(im_shapes).max(axis=0)
        return max_shape

    def __len__(self):
        return self.n_imgrels

    def __getitem__(self, index):

        objdet_res = False
        # while True:
        #   if self._cur >= self.n_imgrels:
        #     if self.wrap_around:
        #       self._cur = 0
        #     else:
        #       raise StopIteration
        #       return

        (im_id, _rels) = self.imgrels[index]

        if im_id is not None:

            rel = deepcopy(_rels)

            # this will be 1 now since we're batching over relationships instead of images
            # TODO: However, the else condition after the following if will never be reached
            n_rel = 1

            if n_rel != 0:
                pass
            elif self.stage == "test":
                if objdet_res != False:
                    return None, None, None, None
                else:
                    return None, None, None
        elif self.stage == "test":
            if objdet_res != False:
                return None, None, None, None
            else:
                return None, None, None

        im = utils.read_img(osp.join(self.dataset.img_dir, im_id))
        ih = im.shape[0]
        iw = im.shape[1]

        image_blob, im_scale = prep_im_for_blob(im, utils.vrd_pixel_means)
        blob = np.zeros(self.max_shape)
        blob[0: image_blob.shape[0], 0:image_blob.shape[1], :] = image_blob
        image_blob = blob
        img_blob = np.zeros((1,) + image_blob.shape, dtype=np.float32)
        img_blob[0] = image_blob

        # TODO: instead of computing obj_boxes_out here, put it in the preprocess
        #  (and maybe transform relationships to contain object indices instead of whole objects)
        # Note: from here on, rel["subject"] and rel["object"] contain indices to objs
        objs = []
        boxes = []

        i_obj = len(objs)
        objs.append(rel["subject"])
        rel["subject"] = i_obj

        i_obj = len(objs)
        objs.append(rel["object"])
        rel["object"] = i_obj

        # this should always be 2
        n_objs = len(objs)

        # Object classes

        if objdet_res != False:
            obj_boxes_out = objdet_res["boxes"]
            obj_classes = objdet_res["classes"]
            pred_confs_img = objdet_res["confs"]  # Note: We don't actually care about the confidence scores here

            n_rel = len(obj_classes) * (len(obj_classes) - 1)

            if n_rel == 0:
                return None, None, None, None
        else:
            obj_boxes_out = np.zeros((n_objs, 4))
            obj_classes = np.zeros((n_objs))

            for i_obj, obj in enumerate(objs):
                obj_boxes_out[i_obj] = utils.bboxDictToNumpy(obj["bbox"])
                obj_classes[i_obj] = obj["id"]

        # Object boxes
        obj_boxes = np.zeros((obj_boxes_out.shape[0], 5))  # , dtype=np.float32)
        obj_boxes[:, 1:5] = obj_boxes_out * im_scale

        # union bounding boxes
        u_boxes = np.zeros((n_rel, 5))

        # the dimension 8 here is the size of the spatial feature vector, containing the relative location and log-distance
        spatial_features = np.zeros((n_rel, 8))
        # TODO: introduce the other spatial feature thingy
        # spatial_features = np.zeros((n_rel, 2, 32, 32))
        #     spatial_features[ii] = [self._getDualMask(ih, iw, sBBox), \
        #               self._getDualMask(ih, iw, oBBox)]

        # TODO: add tiny comment...
        semantic_features = np.zeros((n_rel, 2 * 300))

        # this will contain the probability distribution of each subject-object pair ID over all 70 predicates
        rel_soP_prior = np.zeros((n_rel, self.dataset.n_pred))
        # print(n_rel)

        # Target output for the network
        target = -1 * np.ones((1, self.dataset.n_pred * n_rel))
        pos_idx = 0
        # target = np.zeros((n_rel, self.n_pred))

        # Indices for objects and subjects
        idx_s, idx_o = [], []

        # print(obj_classes)

        if objdet_res != False:
            i_rel = 0
            for s_idx, sub_cls in enumerate(obj_classes):
                for o_idx, obj_cls in enumerate(obj_classes):
                    if (s_idx == o_idx):
                        continue
                    # Subject and object local indices (useful when selecting ROI results)
                    idx_s.append(s_idx)
                    idx_o.append(o_idx)

                    # Subject and object bounding boxes
                    sBBox = obj_boxes_out[s_idx]
                    oBBox = obj_boxes_out[o_idx]

                    # get the union bounding box
                    rBBox = utils.getUnionBBox(sBBox, oBBox, ih, iw)

                    # store the union box (= relation box) of the union bounding box here, with the id i_rel_inst
                    u_boxes[i_rel, 1:5] = np.array(rBBox) * im_scale

                    # store the scaled dimensions of the union bounding box here, with the id i_rel
                    spatial_features[i_rel] = utils.getRelativeLoc(sBBox, oBBox)

                    # semantic features of obj and subj
                    # semantic_features[i_rel] = utils.getSemanticVector(objs[rel["subject"]]["name"], objs[rel["object"]]["name"], self.w2v_model)
                    semantic_features[i_rel] = np.zeros(600)

                    # store the probability distribution of this subject-object pair from the soP_prior
                    rel_soP_prior[i_rel] = self.soP_prior[sub_cls][obj_cls]

                    i_rel += 1
        else:
            # Subject and object local indices (useful when selecting ROI results)
            idx_s.append(rel["subject"])
            idx_o.append(rel["object"])

            # Subject and object bounding boxes
            sBBox = utils.bboxDictToNumpy(objs[rel["subject"]]["bbox"])
            oBBox = utils.bboxDictToNumpy(objs[rel["object"]]["bbox"])

            # get the union bounding box
            rBBox = utils.getUnionBBox(sBBox, oBBox, ih, iw)

            # store the union box (= relation box) of the union bounding box here, with the id i_rel_inst
            u_boxes[0, 1:5] = np.array(rBBox) * im_scale

            # store the scaled dimensions of the union bounding box here, with the id i_rel
            spatial_features[0] = utils.getRelativeLoc(sBBox, oBBox)

            # semantic features of obj and subj
            # semantic_features[i_rel] = utils.getSemanticVector(objs[rel["subject"]]["name"], objs[rel["object"]]["name"], self.w2v_model)
            semantic_features[0] = np.zeros(600)

            # store the probability distribution of this subject-object pair from the soP_prior
            s_cls = objs[rel["subject"]]["id"]
            o_cls = objs[rel["object"]]["id"]
            rel_soP_prior[0] = self.soP_prior[s_cls][o_cls]

            # TODO: this target is not the one we want
            # target[i_rel][rel["predicate"]["id"]] = 1.
            # TODO: enable multi-class predicate (rel_classes: list of predicates for every pair)
            rel_classes = [rel["predicate"]["id"]]
            for rel_label in rel_classes:
                # target[0, pos_idx] = i_rel * self.dataset.n_pred + rel_label
                target[0, pos_idx] = rel_label
                pos_idx += 1

        obj_classes_out = obj_classes

        # Note: the transpose should move the color channel to being the
        # last dimension
        img_blob = torch.FloatTensor(img_blob).permute(0, 3, 1, 2)  # \.cuda()
        obj_boxes = torch.FloatTensor(obj_boxes)  # \.cuda()
        u_boxes = torch.FloatTensor(u_boxes)  # \.cuda()
        idx_s = torch.LongTensor(idx_s)  # \.cuda()
        idx_o = torch.LongTensor(idx_o)  # \.cuda()
        spatial_features = torch.FloatTensor(spatial_features)  # \.cuda()
        semantic_features = torch.FloatTensor(semantic_features)  # \.cuda()
        obj_classes = torch.LongTensor(obj_classes)  # \.cuda()

        # rel_soP_prior = torch.FloatTensor(rel_soP_prior).cuda()
        target = torch.LongTensor(target)  # .cuda()

        net_input = (img_blob, obj_boxes, u_boxes, idx_s, idx_o, spatial_features, obj_classes \
                     # , semantic_features  \
                     )

        if self.stage == "train":
            return net_input, rel_soP_prior, target
        elif self.stage == "test":
            if objdet_res == False:
                return net_input, obj_classes_out, obj_boxes_out
            else:
                return net_input, obj_classes_out, rel_soP_prior, obj_boxes_out


ds_info = {"ds_name": "vrd", "with_bg_obj": False, "with_bg_pred": False}
datalayer = VRDDataLayer(ds_info, "train", shuffle=False)
a = datalayer[0]

# for a in datalayer:
#     for elem in a[0]:
#         print(elem.shape)
#         # imshow(elem[0])
#     print(a[1].shape)
#     print(a[2].shape)
#     print("-"*10)

# this only works with batch size 1!
train_generator = data.DataLoader(
    dataset=datalayer,
    drop_last=True,
    batch_size=256,
    shuffle=True
)

for elem in train_generator:
    print(elem)