import numpy as np
from inferno.utils import python_utils as pyu
# import zipfile
import os  # , io
# from PIL import Image
# from scipy import ndimage
import h5py
from torch.utils.data.dataset import Dataset
from torch.utils.data import DataLoader
from random import choice
from ..transform import Compose
from ..transform.image import FineRandomRotations, RandomScaleSegmentation
from ..transform.generic import Normalize, NormalizeRange  # , Cast, AsTorchBatch
from scipy.ndimage import grey_opening


class AccumulateTransformOverLabelers(object):
    accumulators = ('mean', 'max', 'min')

    def __init__(self, transform, accumulator='mean', close_channels=None):
        self.transform = transform
        assert accumulator in self.accumulators
        if accumulator == 'mean':
            self.accumulator = np.mean
        elif accumulator == 'max':
            self.accumulator = np.amax
        elif accumulator == 'min':
            self.accumulator = np.amin
        if close_channels is not None:
            assert isinstance(close_channels, (list, tuple))
        self.close_channels = close_channels

    def __call__(self, input_):
        transformed = np.array([self.transform(inp) for inp in input_])
        transformed = self.accumulator(transformed, axis=0)
        if self.close_channels is not None:
            for c in self.close_channels:
                # TODO figure out what exactly size does
                transformed[c] = grey_opening(transformed[c], size=(3, 3))
        return transformed


def get_label_transforms(offsets, accumulator='mean', close_channels=None):
    from neurofire.transforms.segmentation import Segmentation2AffinitiesFromOffsets
    seg2aff = Segmentation2AffinitiesFromOffsets(dim=2,
                                                 offsets=pyu.from_iterable(offsets),
                                                 add_singleton_channel_dimension=True,
                                                 retain_segmentation=False)
    return AccumulateTransformOverLabelers(seg2aff, accumulator, close_channels)


def get_joint_transforms(offsets, split='train'):
    from ..transform.image import RandomFlip, RandomRotate, RandomTranspose
    from neurofire.transform.segmentation import ManySegmentationsToFuzzyAffinities
    if split == 'train':
        trafos = Compose(RandomFlip(allow_ud_flips=False),
                     RandomScaleSegmentation((0.8, 1.2)),
                     FineRandomRotations(15),
                     ManySegmentationsToFuzzyAffinities(dim=2, offsets=offsets,
                                                        retain_segmentation=True),
                     )
    else:
        trafos = Compose(ManySegmentationsToFuzzyAffinities(dim=2, offsets=offsets,
                                                        retain_segmentation=True))
    return trafos


def get_bsd500_dataset(root_folder, offsets, split="train", return_no_labels=False):
    return BSD500(root_folder,
                   split=split,
                   return_no_labels=return_no_labels,
                   joint_transform=get_joint_transforms(offsets, split=split))

# TODO return data loaders for train, val and test
def get_bsd500_loaders(root_folder, offsets, shuffle=True, split="all", num_workers=0):
    if split != "all":
        return DataLoader(BSD500(root_folder,
                   split=split,
                   joint_transform=get_joint_transforms(offsets, split=split)))

    splits = ['train', 'val', 'test']

    ds = [BSD500(root_folder,
                       split=s,
                       joint_transform=get_joint_transforms(offsets, split=s))\
                       for s in splits]

    dl = [DataLoader(bsd_split, shuffle=shuffle, num_workers=num_workers) for bsd_split in ds]
    return dl


BSD500_MEAN = np.array([110.797, 113.003, 93.5948], dtype='float32')
BSD500_STD = np.array([63.6275, 59.7415, 61.6398], dtype='float32')

class BSD500(Dataset):
    # BSD contains landscape and portrait images, with up to 9 segmentations
    # This dataset loader returns tuples of images in original orientation and 
    # associated segmentations, which means, that one can not simply
    # create a batch with size > 1 that has a consistent shape
    # For now we assume batch size = 1
    splits = ('train', 'val', 'test')

    # measured on train split


    def __init__(self,
                 root_folder,
                 split='train',
                 return_no_labels=False,
                 image_transform=None,
                 joint_transform=None,
                 label_transform=None):
        """
        Parameters:
        root_folder: folder that contains 'groundTruth' and 'images'  of the BSD 500.
        (http://www.eecs.berkeley.edu/Research/Projects/CS/vision/grouping/BSR/BSR_bsds500.tgz)
        """
        assert os.path.exists(root_folder)
        assert split in self.splits, str(split)

        self.root_folder = root_folder
        if not os.path.exists(os.path.join(root_folder, "BSD500.h5")):
            print("creating h5 files of BSD500")

            import scipy.io as sio
            from scipy.ndimage import imread
            from glob import glob

            with h5py.File(os.path.join(root_folder, "BSD500.h5"), "w") as out:
                for ds in ["train", "val", "test"]:
                    for i, f in enumerate(glob(root_folder + "/groundTruth/" + ds + "/*.mat")):
                        im_path = f.replace("groundTruth", "images").replace(".mat", ".jpg")
                        img = imread(im_path).transpose(2, 0, 1).astype(np.float32)[:, :, :]
                        # normalize
                        img -= BSD500_MEAN[:, None, None]
                        img /= BSD500_STD[:, None, None]

                        image_shape = np.array(img[0].shape)
                        pad_l = (512 - image_shape) // 2 
                        pad_r = 512 - image_shape - pad_l
                        padding = [(0,0)] + list(zip(pad_l, pad_r))
                        out.create_dataset("{}/padding/{}".format(ds, i),
                                           data=np.array(padding))

                        out.create_dataset("{}/image_data/{}".format(ds, i),
                                           data=np.pad(img, padding, 'constant'))
                        all_segmentations = sio.loadmat(f)["groundTruth"][0]
                        best_gt_index = np.argmax([len(np.unique(s)) for s in all_segmentations])
                        print(all_segmentations)
                        label_img = all_segmentations[:, 0][0][0]
                        out.create_dataset("{}/label_data/{}".format(ds, i),
                                           data=label_img)

                        stacked_segmentations = np.stack([s[0][0][0] for s in all_segmentations])\
                                                                .astype(np.float32)[:, :, :]
                        # add one to segmentation such that 0 can be treated as ignore label
                        label_img = np.pad(stacked_segmentations+1, padding, 'constant')
                        out.create_dataset("{}/label_data/{}".format(ds, i),
                                           data=label_img)

        self.root_folder = root_folder
        self.split = split
        self.return_no_labels = return_no_labels

        self.image_transform = image_transform
        self.joint_transform = joint_transform
        self.label_transform = label_transform
        self.load_data()

    def load_data(self):
        self.data = []
        with h5py.File(os.path.join(self.root_folder, "BSD500.h5"), "r") as bsd:
            base = "{}/image_data/".format(self.split)
            label_base = base.replace("image_data", "label_data")

            for img_num in bsd["{}/image_data/".format(self.split)]:

                # load the image
                img_path = base + img_num
                img = bsd[img_path].value

                # load the groundtruths
                gt_path = label_base + img_num
                gt = bsd[gt_path].value

                self.data.append((img, gt))

    def __getitem__(self, index):
        img, gt = self.data[index]

        if self.image_transform is not None:
            img = self.image_transform(img)

        if self.return_no_labels:
            return img

        if self.joint_transform is not None:
            img, gt = self.joint_transform(img, gt)

        if self.label_transform is not None:
            gt = self.label_transform(gt)

        return img, gt

    def __len__(self):
        return len(self.data)


if __name__ == '__main__':
    ROOT_FOLDER = "/home/swolf/local/data/BSDS500/data"
    ds = BSD500(ROOT_FOLDER)
    print(ds[0])
