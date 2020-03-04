import os
import cv2
import numpy as np

# TODO: figure out what pixel means to use, how to compute them:
#  do they come from the dataset used for training, perhaps?
vrd_pixel_means = np.array([[[102.9801, 115.9465, 122.7717]]])


# Bbox as a dict to numpy array
def bboxDictToNumpy(bbox_dict):
  return np.array([bbox_dict["xmin"],
                    bbox_dict["ymin"],
                    bbox_dict["xmax"],
                    bbox_dict["ymax"]])


# Union box of two boxes
def getUnionBBox(aBB, bBB, ih, iw, margin=10):
  return [max(0, min(aBB[0], bBB[0]) - margin),
          max(0, min(aBB[1], bBB[1]) - margin),
          min(iw, max(aBB[2], bBB[2]) + margin),
          min(ih, max(aBB[3], bBB[3]) + margin)]

# Relative location spatial feature
def getRelativeLoc(aBB, bBB):
  sx1, sy1, sx2, sy2 = aBB.astype(np.float32)
  ox1, oy1, ox2, oy2 = bBB.astype(np.float32)
  sw, sh, ow, oh = sx2 - sx1, sy2 - sy1, ox2 - ox1, oy2 - oy1
  xy = np.array([(sx1 - ox1) / ow, (sy1 - oy1) / oh, (ox1 - sx1) / sw, (oy1 - sy1) / sh])
  wh = np.log(np.array([sw / ow, sh / oh, ow / sw, oh / sh]))
  return np.hstack((xy, wh))

# Get word embedding of subject and object label and concatenate them
def getSemanticVector(subject_label, object_label, w2v_model):
  subject_vector = w2v_model[subject_label]
  object_vector = w2v_model[object_label]
  combined_vector = np.concatenate((subject_vector, object_vector), axis=0)
  return combined_vector

# Wrapper for cv2.imread
def read_img(im_file):
  """ Wrapper for cv2.imread """
  if not os.path.exists(im_file):
    raise Exception("Image file not found: " + im_file)
  return np.array(cv2.imread(im_file))

class AverageMeter(object):
  """ Computes and stores the average and current value """
  def __init__(self):
    self.reset()

  def reset(self):
    self.val = 0
    self.avg = 0
    self.sum = 0
    self.count = 0

  def update(self, val, n=1):
    self.val = val
    self.sum += val * n
    self.count += n
    self.avg = self.sum / self.count
