import numpy as np
import os.path as osp
import scipy.io as sio
import scipy

import pickle
from lib.blob import prep_im_for_blob
from lib.dataset import dataset # TODO: return VRDDataset
import torch
import random
# from gensim.models import KeyedVectors
import warnings

import utils, globals
from copy import copy, deepcopy
# TODO: expand so that it supports batch sizes > 1
from torch.utils import data

# TODO class VRDDataLayer(data.Dataset):
class VRDDataLayer():
  """ Iterate through the dataset and yield the input and target for the network """

  def __init__(self, data_info, stage, shuffle = False):

    # There's never need to shuffle in testing
    if shuffle and stage != "train":
      warnings.warn("Shuffling during '{}' was prevented".format(stage), UserWarning)
      shuffle = False

    self.ds_args = utils.data_info_to_data_args(data_info)
    self.stage   = stage
    self.shuffle = shuffle



    self.dataset = dataset(**self.ds_args)
    self.n_obj   = self.dataset.n_obj
    self.n_pred  = self.dataset.n_pred

    self.soP_prior = self.dataset.getDistribution(type="soP", force=True)

    self.imgrels   = deepcopy(self.dataset.getRelst(self.stage))

    # TODO: check if this works
    # Ignore None elements during training
    if self.stage == "train":
      self.imgrels = [(k,v) for k,v in self.imgrels if k != None]

    if self.shuffle:
      random.shuffle(self.imgrels)

    self.N = len(self.imgrels)
    self._cur = 0
    self.wrap_around = ( self.stage == "train" )

    # self.objdet_res = False

    # TODO: restore this
    # print("Loading Word2Vec model...")
    # self.w2v_model = KeyedVectors.load_word2vec_format(osp.join(globals.data_dir, globals.w2v_model_path), binary=True)

  """
  TODO
  def __len__(self):
    return self.N

  def __getitem__(self, index):

-  def next(self, objdet_res = False):
-
-    while True:
-      if self._cur >= self.n_imgrels:
-        if self.wrap_around:
-          self._cur = 0
-        else:
-          raise StopIteration
-          return
-
-      (im_id, _rels) = self.imgrels[self._cur]
-
-      self._cur += 1
-
-      if im_id is not None:
-
-        rels = deepcopy(_rels)
-
-        n_rel = len(rels)
-
-        if n_rel != 0:
-          break
-        elif self.stage == "test":
-          if objdet_res != False:
-            return None, None, None, None
-          else:
-            return None, None, None
+​
+  def __getitem__(self, index):
+​
+    objdet_res = False
+    # while True:
+    #   if self._cur >= self.n_imgrels:
+    #     if self.wrap_around:
+    #       self._cur = 0
+    #     else:
+    #       raise StopIteration
+    #       return
+​
+    (im_id, _rels) = self.imgrels[index]
+​
+    if im_id is not None:
+​
+      rels = deepcopy(_rels)
+​
+      n_rel = len(rels)
+​
+      if n_rel != 0:
+        pass
       elif self.stage == "test":
         if objdet_res != False:
           return None, None, None, None
         else:
           return None, None, None
-
-
+    elif self.stage == "test":
+      if objdet_res != False:
+        return None, None, None, None
+      else:
+        return None, None, None
+​
  """

  def next(self, objdet_res = False):

    while True:
      if self._cur >= self.N:
        if self.wrap_around:
          self._cur = 0
        else:
          raise StopIteration
          return

      (im_id, _rels) = self.imgrels[self._cur]

      self._cur += 1

      if im_id is not None:

        rels = deepcopy(_rels)

        n_rel = len(rels)

        if n_rel != 0:
          break
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
    img_blob = np.zeros((1,) + image_blob.shape, dtype=np.float32)
    img_blob[0] = image_blob


    # TODO: instead of computing obj_boxes_out here, put it in the preprocess
    #  (and maybe transform relationships to contain object indices instead of whole objects)
    # Note: from here on, rel["subject"] and rel["object"] contain indices to objs

    ####
    # ATTENTION: this was maybe leading to troubles!!! "Duplicate" objects are added twice!
    # Another possible reason of failing is: our bboxDictToList has a different order
    ####
    # TODO: switch to annos:
    #  subject -> sub
    #  object -> obj
    #  obj"id" -> obj"cls"
    #  "predicate"{id,name} -> "pred" (id)
    #  no need for bboxDictToNumpy anymore, it's a list, maybe create a tensor directly?
    objs = []
    for i_rel, rel in enumerate(rels):

      i_obj = len(objs)
      objs.append(rel["subject"])
      rel["subject"] = i_obj

      i_obj = len(objs)
      objs.append(rel["object"])
      rel["object"] = i_obj

    n_objs = len(objs)

    # Object classes

    if objdet_res != False:
      obj_boxes_out  = objdet_res["boxes"]
      obj_classes    = objdet_res["classes"]
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
          if(s_idx == o_idx):
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
      for i_rel, rel in enumerate(rels):

        # Subject and object local indices (useful when selecting ROI results)
        idx_s.append(rel["subject"])
        idx_o.append(rel["object"])

        # Subject and object bounding boxes
        sBBox = utils.bboxDictToNumpy(objs[rel["subject"]]["bbox"])
        oBBox = utils.bboxDictToNumpy(objs[rel["object"]]["bbox"])

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
        s_cls = objs[rel["subject"]]["id"]
        o_cls = objs[rel["object"]]["id"]
        rel_soP_prior[i_rel] = self.soP_prior[s_cls][o_cls]

        # TODO: this target is not the one we want
        # target[i_rel][rel["predicate"]["id"]] = 1.
        # TODO: enable multi-class predicate (rel_classes: list of predicates for every pair)
        rel_classes = [rel["predicate"]["id"]]
        for rel_label in rel_classes:
          target[0, pos_idx] = i_rel * self.dataset.n_pred + rel_label
          pos_idx += 1


    obj_classes_out = obj_classes

    # Note: the transpose should move the color channel to being the
    #  last dimension
    img_blob          = torch.FloatTensor(img_blob,          device=utils.device).permute(0, 3, 1, 2)
    obj_boxes         = torch.FloatTensor(obj_boxes,         device=utils.device)
    u_boxes           = torch.FloatTensor(u_boxes,           device=utils.device)
    idx_s             = torch.LongTensor(idx_s,              device=utils.device)
    idx_o             = torch.LongTensor(idx_o,              device=utils.device)
    spatial_features  = torch.FloatTensor(spatial_features,  device=utils.device)
    semantic_features = torch.FloatTensor(semantic_features, device=utils.device)
    obj_classes       = torch.LongTensor(obj_classes,        device=utils.device)

    # rel_soP_prior = torch.FloatTensor(rel_soP_prior, device=utils.device)
    target        = torch.LongTensor(target,                 device=utils.device)

    net_input = (img_blob           \
               , obj_boxes          \
               , u_boxes            \
               , idx_s              \
               , idx_o              \
               , spatial_features   \
               , obj_classes        \
              #, semantic_features  \
      )

    if self.stage == "train":
      return net_input      \
            , rel_soP_prior \
            , target
    elif self.stage == "test":
      if objdet_res == False:
        return net_input          \
              , obj_classes_out   \
              , obj_boxes_out
      else:
        return net_input          \
              , obj_classes_out   \
              , rel_soP_prior     \
              , obj_boxes_out

"""
ds_info = {"ds_name": "vrd", "with_bg_obj": False, "with_bg_pred": False}
datalayer = VRDDataLayer(ds_info, "train", shuffle=False)
a = datalayer[0]
​
# this only works with batch size 1!
train_generator = data.DataLoader(
    dataset=datalayer,
    drop_last=True,
    batch_size=1,
    shuffle=True
)
"""

"""
...data.IterableDataset

  def __init__(self, data_info, stage, shuffle = False):
    super(VRDDataLayer).__init__()
    ...
  ...
  def __iter__(self):
    worker_info = torch.utils.data.get_worker_info()
    if worker_info is None:  # single-process data loading, return the full iterator
      iter_start = self.start
      iter_end = self.end
    else:  # in a worker process
      # split workload
      per_worker = int(math.ceil((self.end - self.start) / float(worker_info.num_workers)))
      worker_id = worker_info.id
      iter_start = self.start + worker_id * per_worker
      iter_end = min(iter_start + per_worker, self.end)
    return iter(range(iter_start, iter_end))

ds = VRDDataLayer(start=3, end=7)

print(list(torch.utils.data.DataLoader(ds, num_workers=self.num_workers... but note that this messes up the order ( see https://pytorch.org/docs/stable/data.html#torch.utils.data.IterableDataset ))))

"""
