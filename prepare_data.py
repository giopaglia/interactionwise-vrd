import sys
import os.path as osp
from glob import glob

from collections import defaultdict

import numpy as np
import scipy.io as sio

import json
import pickle
import globals
import utils

"""
This script prepares the data from the input format to the one we want.
Each dataset has potentially a different format, so we use an abstract class DataPreparer
and introduce a different class for handling each input format.

Notes:
- The granularity (either 'img' or 'rel') of an output format refers to whether
-  the relationships are grouped by image or not.
- An image "annos" is something like {"objects" : [obj1, obj2, ...], "relationships" : [rel1, rel2, ...]}
- An image "relst" is something like [rel1, rel2, ...]
- When a database is loaded with load_data, it can be loaded in any convenient format and the flag
   self.cur_format will be set accordingly
- Then, one can switch from one format to the other with databse-independent functions.
  This makes sure that the data is the same across different formats

"""

class DataPreparer:
    def __init__(self):
        self.objects_vocab_file    = "objects.json"
        self.predicates_vocab_file = "predicates.json"

        self.cur_format = None

    # This function reads the dataset's vocab txt files and loads them
    def prepare_vocabs(self, obj_vocab_file, pred_vocab_file):
        self.writejson(utils.load_txt_list(self.fullpath(obj_vocab_file)),  self.objects_vocab_file)
        self.writejson(utils.load_txt_list(self.fullpath(pred_vocab_file)), self.predicates_vocab_file)
        self.obj_vocab  = self.readjson(self.objects_vocab_file)
        self.pred_vocab = self.readjson(self.predicates_vocab_file)

    # This function converts to relst
    def _generate_relst(self, anns): pass
    def _generate_annos(self, anns): pass

    # Save data
    def save_data(self, format, granularity = "img"):
        self.to_format(format)

        output_file_format = "data_{}_{}_{{}}.json".format(format, granularity)

        vrd_data_train = [(k, self.vrd_data[k]) if k is not None else (None, None) for k in self.splits["train"]]
        vrd_data_test  = [(k, self.vrd_data[k]) if k is not None else (None, None) for k in self.splits["test"]]

        if granularity == "rel":
            assert format == "relst", "Mh. Does it make sense to granulate 'rel' with format {}?".format(format)
            def granulate(d):
                new_vrd_data = []
                for (img_path, relst) in d:
                    if img_path is None:
                        new_vrd_data.append((None, None))
                        continue
                    for img_rel in relst:
                        new_vrd_data.append((img_path,img_rel))
                return new_vrd_data
            vrd_data_train = granulate(vrd_data_train)
            vrd_data_test  = granulate(vrd_data_test)
        elif granularity == "img":
            pass
        else:
            raise ValueError("Error. Unknown granularity: {}".format(granularity))

        self.writejson(vrd_data_train, output_file_format.format("train"))
        self.writejson(vrd_data_test,  output_file_format.format("test"))

    # transform vrd_data to the desired format
    def to_format(self, to_format):
        if to_format == self.cur_format:
            return
        elif self.cur_format == "relst" and to_format == "annos":
            self.relst2annos()
        elif self.cur_format == "annos" and to_format == "relst":
            self.annos2relst()
        else:
            raise NotImplementedError("Unknown format conversion: {} -> {}".format(self.cur_format, to_format))

    # "annos" format separates objects and relatinoships
    def relst2annos(self):
        new_vrd_data = {}
        for img_path, relst in self.vrd_data.items():

            objects  = []
            rels     = []
            def add_object(obj):
                for i_obj,any_obj in enumerate(objects):
                    if any_obj["cls"]   == obj["id"] and \
                       np.all(any_obj["bbox"] == utils.bboxDictToList(obj["bbox"])):
                       return i_obj
                i_obj = len(objects)
                objects.append({"cls" : obj["id"], "bbox" : utils.bboxDictToList(obj["bbox"])})
                return i_obj

            for i_rel, rel in enumerate(relst):
                new_rel = {}

                new_rel["sub"] = add_object(rel["subject"])
                new_rel["obj"]  = add_object(rel["object"])

                new_rel["pred"] = rel["predicate"]["id"]
                rels.append(new_rel)

            new_vrd_data[img_path] = {
                "objs" : objects,
                "rels" : rels
            }

        self.vrd_data = new_vrd_data
        self.cur_format = "annos"

    # def annos2relst(self):
    #     TODO
    #     self.cur_format = "relst"

    def readbbox_arr(self, bbox, margin=0):
        return {
            'ymin': (bbox[0]-margin),
            'ymax': (bbox[1]-margin),
            'xmin': (bbox[2]-margin),
            'xmax': (bbox[3]-margin)
        }


    # INPUT/OUTPUT Helpers
    def fullpath(self, filename):
        return osp.join(globals.data_dir, self.metadata_subfolder, filename)

    # plain files
    def readfile(self, filename):
        return open(self.fullpath(filename), 'r')
    def writefile(self, filename):
        return open(self.fullpath(filename), 'w')
    # json files
    def readjson(self, filename):
        with self.readfile(filename) as rfile:
            return json.load(rfile)
        return open(self.fullpath(filename), 'w')
    def writejson(self, obj, filename):
        with self.writefile(filename) as f:
            json.dump(obj, f)
    # pickle files
    def readpickle(self, filename):
        with open(self.fullpath(filename), 'rb') as f:
            return pickle.load(f, encoding="latin1")
    def writepickle(self, obj, filename):
        with open(self.fullpath(filename), 'wb') as f:
            pickle.dump(obj, f)
    # matlab files
    def readmat(self, filename): return sio.loadmat(self.fullpath(filename))


class VRDPrep(DataPreparer):
    def __init__(self):
        super(VRDPrep, self).__init__()

        self.metadata_subfolder = "vrd"

        self.train_dsr = self.readpickle("train.pkl")
        self.test_dsr  = self.readpickle("test.pkl")

        self.prepare_vocabs("obj.txt", "rel.txt")

        # TODO: Additionally handle files like {test,train}_image_metadata.json

        # LOAD DATA
        annotations_train = self.readjson("annotations_train.json")
        annotations_test  = self.readjson("annotations_test.json")

        # Transform img file names to img subpaths
        annotations = {}
        for img_file,anns in annotations_train.items():
            annotations[osp.join("sg_train_images", img_file)] = anns
        for img_file,anns in annotations_test.items():
            annotations[osp.join("sg_test_images", img_file)] = anns

        vrd_data = {}
        for img_path, anns in annotations.items():
            vrd_data[img_path] = self._generate_relst(anns)

        self.vrd_data = vrd_data
        self.cur_format = "relst"

        self.splits = {
            "train" : [osp.join(*x["img_path"].split("/")[-2:]) if x is not None else None for x in self.train_dsr],
            "test"  : [osp.join(*x["img_path"].split("/")[-2:]) if x is not None else None for x in self.test_dsr],
        }


    def _generate_relst(self, anns):
        relst = []
        for ann in anns:
            subject_id = ann['subject']['category']
            subject_label = self.obj_vocab[subject_id]
            # this is as per the format described in the README of the VRD dataset
            subject_bbox = self.readbbox_arr(ann['subject']['bbox'], 1)

            object_id = ann['object']['category']
            object_label = self.obj_vocab[object_id]
            object_bbox = self.readbbox_arr(ann['object']['bbox'], 1)

            predicate_id = ann['predicate']
            predicate_label = self.pred_vocab[predicate_id]

            rel_data = defaultdict(lambda: dict())
            rel_data['subject']['id']   = subject_id
            rel_data['subject']['name'] = subject_label
            rel_data['subject']['bbox'] = subject_bbox

            rel_data['object']['id']   = object_id
            rel_data['object']['name'] = object_label
            rel_data['object']['bbox'] = object_bbox

            rel_data['predicate']['name'] = predicate_label
            rel_data['predicate']['id'] = predicate_id

            if rel_data not in relst:
                relst.append(rel_data)
            # else:
            #     print("Found duplicate relationship in image: {}".format(img_path))

        return relst

    def loadsave_relst_dsr(self):
        """
            This function loads the {train,test}.pkl which contains the original format of the data
            from the vrd-dsr repo, and converts it to the relst format.
        """

        for (stage,src_data) in [("train",self.train_dsr),("test",self.test_dsr)]:
            vrd_data = []
            for elem in src_data:
                if elem is None:
                    vrd_data.append((None,None))
                    continue
                img_path = osp.join(*elem['img_path'].split("/")[-2:])
                bounding_boxes = elem['boxes']
                subjects = elem['ix1']
                objects = elem['ix2']
                classes = elem['classes']
                relations = elem['rel_classes']
                relationships = []
                for index in range(len(relations)):
                    subject_id = classes[subjects[index]]
                    subject_label = self.obj_vocab[subject_id]
                    subject_bbox = {
                        'ymin': int(bounding_boxes[subjects[index]][1]),
                        'ymax': int(bounding_boxes[subjects[index]][3]),
                        'xmin': int(bounding_boxes[subjects[index]][0]),
                        'xmax': int(bounding_boxes[subjects[index]][2])
                    }

                    object_id = classes[objects[index]]
                    object_label = self.obj_vocab[object_id]
                    object_bbox = {
                        'ymin': int(bounding_boxes[objects[index]][1]),
                        'ymax': int(bounding_boxes[objects[index]][3]),
                        'xmin': int(bounding_boxes[objects[index]][0]),
                        'xmax': int(bounding_boxes[objects[index]][2])
                    }

                    for pred in relations[index]:
                        predicate_id = pred
                        predicate_label = self.pred_vocab[predicate_id]

                        rel_data = defaultdict(lambda: dict())
                        rel_data['subject']['id'] = int(subject_id)
                        rel_data['subject']['name'] = subject_label
                        rel_data['subject']['bbox'] = subject_bbox

                        rel_data['object']['id'] = int(object_id)
                        rel_data['object']['name'] = object_label
                        rel_data['object']['bbox'] = object_bbox

                        rel_data['predicate']['id'] = int(predicate_id)
                        rel_data['predicate']['name'] = predicate_label

                        relationships.append(dict(rel_data))

                if len(relationships) > 0:
                    vrd_data.append((img_path,relationships))
                else:
                    print(img_path)

            # TODO: uniform this to the others so that we can use it normally (just save it with a different output name)
            self.writejson(vrd_data, "dsr_relst_{}.json".format(stage))


    # This function creates the pickles used for the evaluation on the for VRD Dataset
    # The ground "truths and object detections are provided by Visual Relationships with Language Priors (files available on GitHub)
    #  as matlab .mat objects.
    def prepareEvalFromLP(self):

        # Input files
        det_result_path = osp.join("eval", "from-language-priors", "det_result.mat")
        gt_path         = osp.join("eval", "from-language-priors", "gt.mat")
        gt_zs_path      = osp.join("eval", "from-language-priors", "zeroShot.mat")

        # Output files
        det_result_output_path = osp.join("eval", "det_res.pkl")
        gt_output_path         = osp.join("eval", "gt.pkl")
        gt_zs_output_path      = osp.join("eval", "gt_zs.pkl")


        det_result = self.readmat(det_result_path)
        # TODO
        #gt         = self.readmat(gt_path)
        #gt_zs      = self.readmat(gt_zs_path)

        assert len(det_result["detection_bboxes"]) == 1, "ERROR. Malformed .mat file"
        assert len(det_result["detection_labels"]) == 1, "ERROR. Malformed .mat file"
        assert len(det_result["detection_confs"])  == 1, "ERROR. Malformed .mat file"

        det_result_pkl = {}
        det_result_pkl["boxes"] = []
        det_result_pkl["cls"]   = []
        det_result_pkl["confs"] = []

        for i,(lp_boxes, lp_cls, lp_confs) in \
                enumerate(zip(det_result["detection_bboxes"][0],
                              det_result["detection_labels"][0],
                              det_result["detection_confs"][0])):

            # The -1s fixes the matlab-is-1-indexed problem
            transf_lp_boxes = lp_boxes-1
            transf_lp_cls   = lp_cls-1
            transf_lp_confs = lp_confs

            det_result_pkl["boxes"].append(np.array(transf_lp_boxes, dtype=np.int))
            det_result_pkl["cls"]  .append(np.array(transf_lp_cls,   dtype=np.int))
            det_result_pkl["confs"].append(np.array(transf_lp_confs, dtype=np.float32))

        self.writepickle(det_result_pkl, det_result_output_path)

class VGPrep(DataPreparer):
    def __init__(self):
        super(VGPrep, self).__init__()

        self.num_objects    = 1600
        self.num_attributes = 400
        self.num_predicates = 20

        self.metadata_subfolder = osp.join("genome", "{}-{}-{}".format(self.num_objects, self.num_attributes, self.num_predicates))

        self.json_selector = osp.join("json", "*.json")

        self.prepare_vocabs("objects_vocab.txt", "relations_vocab.txt")

        # LOAD DATA
        objects_label_to_id_mapping    = utils.invert_dict(self.obj_vocab)
        predicates_label_to_id_mapping = utils.invert_dict(self.pred_vocab)

        vrd_data = {}
        for ix, filename in enumerate(glob(self.fullpath(self.json_selector))):
            data = self.readjson(filename)

            img_path = osp.join(data['folder'], data['filename'])

            vrd_data[img_path] = self._generate_relst(data, objects_label_to_id_mapping, predicates_label_to_id_mapping)

        self.vrd_data = vrd_data
        self.cur_format = "relst"

        self.splits = {
            "train" : [line.split(" ")[0] for line in utils.load_txt_list(self.fullpath("../train.txt"))],
            "test"  : [line.split(" ")[0] for line in utils.load_txt_list(self.fullpath("../test.txt"))],
        }

    def _generate_relst(self, data):
        objects_info = {}
        for obj in data['objects']:
            obj_id = obj['object_id']
            objects_info[obj_id] = {
                'name': obj['name'][0],
                'bbox': {k: int(v) for k, v in obj['bndbox'].items()}
            }

        relst = []
        for pred in data['relations']:
            subject_info = objects_info[pred['subject_id']]
            object_info = objects_info[pred['object_id']]
            pred_label = pred['predicate']

            rel_data = defaultdict(lambda: dict())
            rel_data['subject']['name'] = subject_info['name']
            rel_data['subject']['id'] = self.obj_vocab[subject_info['name']]
            rel_data['subject']['bbox'] = subject_info['bbox']

            rel_data['object']['name'] = object_info['name']
            rel_data['object']['id'] = self.obj_vocab[object_info['name']]
            rel_data['object']['bbox'] = object_info['bbox']

            rel_data['predicate']['name'] = pred_label
            rel_data['predicate']['id'] = self.pred_vocab[pred_label]

            rel_data = dict(rel_data)
            if rel_data not in relst:
                relst.append(rel_data)

        return relst

if __name__ == '__main__':

    # select whether to generate image_rels or annotations
    # if true,  image_rels  will be generated
    # if false, annotations will be generated

    data_preparer_vrd = VRDPrep()
    data_preparer_vrd.prepareEvalFromLP()
    data_preparer_vrd.save_data("relst")
    # This is to generate the data in relst format using the original annotations in VRD
    # If batching is set to True, each relationship within an image will be a separate instance, as
    # opposed to a set of relationships within an image instance. This is so to facilitate proper
    # batching via the PyTorch Dataloader
    data_preparer_vrd.save_data("relst", "rel")
    data_preparer_vrd.save_data("annos")

    # Generate the data in relst format using the {train,test}.pkl files provided by DSR
    data_preparer_vrd.loadsave_relst_dsr()

    """
    # TODO: test to see if VG preparation works
    data_preparer_vg  = VGPrep()
    data_preparer_vrd.save_data("annos")
    data_preparer_vrd.save_data("relst")
    data_preparer_vrd.save_data("relst", "rel")
    """
