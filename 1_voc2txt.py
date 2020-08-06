import xml.etree.ElementTree as ET
import pickle
import os
from tqdm import tqdm 
from os import listdir, getcwd
from os.path import join

classes = ["red_stop", "green_go", "yellow_back", "pedestrian_crossing", "speed_limited", "speed_unlimited"]

rootpath = "."
xmlpath = os.path.join(rootpath, "xml") 

def convert(size, box):
    dw = 1./(size[0])
    dh = 1./(size[1])
    x = (box[0] + box[1])/2.0 - 1
    y = (box[2] + box[3])/2.0 - 1
    w = box[1] - box[0]
    h = box[3] - box[2]
    x = x*dw
    w = w*dw
    y = y*dh
    h = h*dh
    return (x,y,w,h)

def convert_annotation(rootpath, xmlfile):  
    with open(xmlfile, "r") as in_file: 
        txtname = 'dataset.txt'
        txtpath = os.path.join(rootpath, "txt") 
        if not os.path.exists(txtpath): os.makedirs(txtpath)
        txtfile = os.path.join(txtpath, txtname)
        with open(txtfile, "a") as out_file:
            tree = ET.parse(in_file)
            root = tree.getroot()
            size = root.find('size')
            w = int(size.find('width').text)
            h = int(size.find('height').text)
            out_file.truncate()
            out_file.write(os.path.split(xmlfile)[-1].replace(".xml", ".jpg") + " ") 
            for obj in root.iter('object'):
                difficult = obj.find('difficult').text
                cls_ = obj.find('name').text
                if cls_ not in classes or int(difficult) == 1:
                    continue
                cls_id = classes.index(cls_)
                xmlbox = obj.find('bndbox')
                b = (float(xmlbox.find('xmin').text), float(xmlbox.find('ymin').text), \
                    float(xmlbox.find('xmax').text), float(xmlbox.find('ymax').text))
                # bb = convert((w,h), b)
                out_file.write(",".join([str(a) for a in b]) + "," + str(cls_id) + " ") 
            out_file.write("\n") 


if __name__ == "__main__":
    path_list = os.listdir(xmlpath)

    # Patch 
    patch_path = "F:\\V008\\train-dataset"
    patch_list = os.listdir(patch_path) 
    print(patch_list)
    input() 
    for i, p in enumerate(path_list):
        if p in patch_list: 
            path_list[i] = os.path.join(patch_path, p)
        else: 
            path_list[i] = os.path.join(xmlpath, p)

    print(path_list) 
    input() 

    txtpath = os.path.join(rootpath, "txt") 
    if not os.path.exists(txtpath): os.makedirs(txtpath)
    with open(os.path.join(txtpath, "classes.txt"), "w+") as f: 
        for label in classes: 
            f.write(label + "\n") 

    for xml in tqdm(path_list): 
        path = xml # os.path.join(xmlpath, xml)
        if '.xml' in path or '.XML' in path:
            convert_annotation(rootpath, path)
