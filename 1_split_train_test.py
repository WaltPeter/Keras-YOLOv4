import os
from random import shuffle
from os import listdir, getcwd
from os.path import join

if __name__ == '__main__':
    base = "annotations"
    dest = os.path.join(base, "train.txt") 
    dest2 = os.path.join(base, "val.txt") 

    if not os.path.exists(base): os.makedirs(base)

    with open("txt/dataset.txt", "r") as f: 
        lines = f.read().split("\n") 

    print(lines)
    input()  

    shuffle(lines) 
    train_test_ratio = 0.85
    a = int(len(lines) * train_test_ratio)  

    train_file = open(dest, 'a')
    val_file = open(dest2, 'a')

    print("train set:", len(lines[:a]))
    print("val set:", len(lines[a:]))

    for line in lines[a:]: 
        val_file.write(line + '\n')
        
    for line in lines[:a]: 
        train_file.write(line + '\n') 

    train_file.close()
    val_file.close()