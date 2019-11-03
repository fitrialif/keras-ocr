# pylint: disable=invalid-name,too-many-locals
import typing
import os

# The PyTorch portions of this code are subject to the following copyright notice.
# Copyright (c) 2019-present NAVER Corp.

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import cv2
import keras
import keras.layers
import keras.backend
import numpy as np
import tensorflow as tf

from . import tools


def compute_input(image):
    # should be RGB order
    image = image.astype('float32')
    mean = np.array([0.485, 0.456, 0.406])
    variance = np.array([0.229, 0.224, 0.225])

    image -= mean * 255
    image /= variance * 255
    return image


def invert_input(X):
    X = X.copy()
    mean = np.array([0.485, 0.456, 0.406])
    variance = np.array([0.229, 0.224, 0.225])

    X *= variance * 255
    X += mean * 255
    return X.clip(0, 255).astype('uint8')


def get_gaussian_heatmap(size=512, distanceRatio=3.34):
    v = np.abs(np.linspace(-size / 2, size / 2, num=size))
    x, y = np.meshgrid(v, v)
    g = np.sqrt(x**2 + y**2)
    g *= distanceRatio / (size / 2)
    g = np.exp(-(1 / 2) * (g**2))
    g *= 255
    return g.clip(0, 255).astype('uint8')


def upconv(x, n, filters):
    x = keras.layers.Conv2D(filters=filters, kernel_size=1, strides=1, name=f'upconv{n}.conv.0')(x)
    x = keras.layers.BatchNormalization(epsilon=1e-5, momentum=0.9, name=f'upconv{n}.conv.1')(x)
    x = keras.layers.Activation('relu', name=f'upconv{n}.conv.2')(x)
    x = keras.layers.Conv2D(filters=filters // 2,
                            kernel_size=3,
                            strides=1,
                            padding='same',
                            name=f'upconv{n}.conv.3')(x)
    x = keras.layers.BatchNormalization(epsilon=1e-5, momentum=0.9, name=f'upconv{n}.conv.4')(x)
    x = keras.layers.Activation('relu', name=f'upconv{n}.conv.5')(x)
    return x


def make_vgg_block(x, filters, n, prefix, pooling=True):
    x = keras.layers.Conv2D(filters=filters,
                            strides=(1, 1),
                            kernel_size=(3, 3),
                            padding='same',
                            name=f'{prefix}.{n}')(x)
    x = keras.layers.BatchNormalization(momentum=0.1, epsilon=1e-5, axis=-1,
                                        name=f'{prefix}.{n+1}')(x)
    x = keras.layers.Activation('relu', name=f'{prefix}.{n+2}')(x)
    if pooling:
        x = keras.layers.MaxPooling2D(pool_size=(2, 2),
                                      padding='valid',
                                      strides=(2, 2),
                                      name=f'{prefix}.{n+3}')(x)
    return x


def compute_maps(heatmap, image_height, image_width, lines):
    assert image_height % 2 == 0, 'Height must be an even number'
    assert image_width % 2 == 0, 'Width must be an even number'

    textmap = np.zeros((image_height // 2, image_width // 2)).astype('float32')
    linkmap = np.zeros((image_height // 2, image_width // 2)).astype('float32')

    src = np.array([[0, 0], [heatmap.shape[1], 0], [heatmap.shape[1], heatmap.shape[0]],
                    [0, heatmap.shape[0]]]).astype('float32')

    for line in lines:
        previous_link_points = None
        for x1, y1, x2, y2, x3, y3, x4, y4, c in line:
            if c == ' ':
                previous_link_points = None
                continue
            yc = (y4 + y1 + y3 + y2) / 4
            xc = (x1 + x2 + x3 + x4) / 4

            current_link_points = np.array([[(xc + (x1 + x2) / 2) / 2, (yc + (y1 + y2) / 2) / 2],
                                            [(xc + (x3 + x4) / 2) / 2,
                                             (yc + (y3 + y4) / 2) / 2]]) / 2
            character_points = np.array([[x1, y1], [x2, y2], [x3, y3], [x4, y4]
                                         ]).astype('float32') / 2
            # pylint: disable=unsubscriptable-object
            if previous_link_points is not None:
                link_points = np.array([
                    previous_link_points[0], current_link_points[0], current_link_points[1],
                    previous_link_points[1]
                ])
                ML = cv2.getPerspectiveTransform(
                    src=src,
                    dst=link_points.astype('float32'),
                )
                linkmap += cv2.warpPerspective(heatmap,
                                               ML,
                                               dsize=(linkmap.shape[1],
                                                      linkmap.shape[0])).astype('float32')
            MA = cv2.getPerspectiveTransform(
                src=src,
                dst=character_points,
            )
            textmap += cv2.warpPerspective(heatmap, MA, dsize=(textmap.shape[1],
                                                               textmap.shape[0])).astype('float32')
            # pylint: enable=unsubscriptable-object
            previous_link_points = current_link_points
    return np.concatenate([textmap[..., np.newaxis], linkmap[..., np.newaxis]], axis=2).clip(
        0, 255) / 255


def map_to_rgb(y):
    return (np.concatenate([y, np.zeros(
        (y.shape[0], y.shape[1], 1))], axis=-1) * 255).astype('uint8')


def drawBoxes(image, boxes, color=(255, 0, 0), thickness=5):
    canvas = image.copy()
    for box in boxes:
        cv2.polylines(img=canvas,
                      pts=box[np.newaxis].astype('int32'),
                      color=color,
                      thickness=thickness,
                      isClosed=True)
    return canvas


def getBoxes(y_pred,
             detection_threshold=0.7,
             text_threshold=0.4,
             link_threshold=0.4,
             size_threshold=10):
    box_groups = []
    for y_pred_cur in y_pred:
        # Prepare data
        textmap = y_pred_cur[..., 0].copy()
        linkmap = y_pred_cur[..., 1].copy()
        img_h, img_w = textmap.shape

        _, text_score = cv2.threshold(textmap,
                                      thresh=text_threshold,
                                      maxval=1,
                                      type=cv2.THRESH_BINARY)
        _, link_score = cv2.threshold(linkmap,
                                      thresh=link_threshold,
                                      maxval=1,
                                      type=cv2.THRESH_BINARY)
        n_components, labels, stats, _ = cv2.connectedComponentsWithStats(np.clip(
            text_score + link_score, 0, 1).astype('uint8'),
                                                                          connectivity=4)
        boxes = []
        for component_id in range(1, n_components):
            # Filter by size
            size = stats[component_id, cv2.CC_STAT_AREA]

            if size < size_threshold:
                continue

            # If the maximum value within this connected component is less than
            # text threshold, we skip it.
            if np.max(textmap[labels == component_id]) < detection_threshold:
                continue

            # Make segmentation map. It is 255 where we find text, 0 otherwise.
            segmap = np.zeros_like(textmap)
            segmap[labels == component_id] = 255
            segmap[np.logical_and(link_score, text_score)] = 0
            x, y, w, h = [
                stats[component_id, key] for key in
                [cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT]
            ]

            # Expand the elements of the segmentation map
            niter = int(np.sqrt(size * min(w, h) / (w * h)) * 2)
            sx, sy = max(x - niter, 0), max(y - niter, 0)
            ex, ey = min(x + w + niter + 1, img_w), min(y + h + niter + 1, img_h)
            segmap[sy:ey, sx:ex] = cv2.dilate(
                segmap[sy:ey, sx:ex],
                cv2.getStructuringElement(cv2.MORPH_RECT, (1 + niter, 1 + niter)))

            # Make rotated box from contour
            contours, _ = cv2.findContours(segmap.astype('uint8'),
                                           mode=cv2.RETR_TREE,
                                           method=cv2.CHAIN_APPROX_SIMPLE)
            contour = contours[0]
            box = cv2.boxPoints(cv2.minAreaRect(contour))

            # Check to see if we have a diamond
            w, h = np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[1] - box[2])
            box_ratio = max(w, h) / (min(w, h) + 1e-5)
            if abs(1 - box_ratio) <= 0.1:
                l, r = contour[:, 0, 0].min(), contour[:, 0, 0].max()
                t, b = contour[:, 0, 1].min(), contour[:, 0, 1].max()
                box = np.array([[l, t], [r, t], [r, b], [l, b]], dtype=np.float32)
            else:
                # Make clock-wise order
                box = np.array(np.roll(box, 4 - box.sum(axis=1).argmin(), 0))
            boxes.append(2 * box)
        box_groups.append(np.array(boxes))
    return box_groups


class UpsampleLike(keras.layers.Layer):
    """ Keras layer for upsampling a Tensor to be the same shape as another Tensor.
    """
    def call(self, inputs, **kwargs):
        source, target = inputs
        target_shape = keras.backend.shape(target)
        if keras.backend.image_data_format() == 'channels_first':
            raise NotImplementedError
        else:
            # pylint: disable=no-member
            return tf.compat.v1.image.resize_bilinear(source,
                                                      size=(target_shape[1], target_shape[2]),
                                                      half_pixel_centers=True)

    def compute_output_shape(self, input_shape):
        if keras.backend.image_data_format() == 'channels_first':
            raise NotImplementedError
        else:
            return (input_shape[0][0], ) + input_shape[1][1:3] + (input_shape[0][-1], )


def build_keras_model(weights_path: str = None):
    inputs = keras.layers.Input((None, None, 3))
    x = make_vgg_block(inputs, filters=64, n=0, pooling=False, prefix='basenet.slice1')
    x = make_vgg_block(x, filters=64, n=3, pooling=True, prefix='basenet.slice1')
    x = make_vgg_block(x, filters=128, n=7, pooling=False, prefix='basenet.slice1')
    x = make_vgg_block(x, filters=128, n=10, pooling=True, prefix='basenet.slice1')
    x = make_vgg_block(x, filters=256, n=14, pooling=False, prefix='basenet.slice2')
    x = make_vgg_block(x, filters=256, n=17, pooling=False, prefix='basenet.slice2')
    x = make_vgg_block(x, filters=256, n=20, pooling=True, prefix='basenet.slice3')
    x = make_vgg_block(x, filters=512, n=24, pooling=False, prefix='basenet.slice3')
    x = make_vgg_block(x, filters=512, n=27, pooling=False, prefix='basenet.slice3')
    x = make_vgg_block(x, filters=512, n=30, pooling=True, prefix='basenet.slice4')
    x = make_vgg_block(x, filters=512, n=34, pooling=False, prefix='basenet.slice4')
    x = make_vgg_block(x, filters=512, n=37, pooling=False, prefix='basenet.slice4')
    x = make_vgg_block(x, filters=512, n=40, pooling=True, prefix='basenet.slice4')
    vgg = keras.models.Model(inputs=inputs, outputs=x)

    # slice1 is 11, slice2 is 18, slice3 is 28, slice4 is 38
    s1 = vgg.get_layer('basenet.slice1.12').output
    s2 = vgg.get_layer('basenet.slice2.19').output
    s3 = vgg.get_layer('basenet.slice3.29').output
    s4 = vgg.get_layer('basenet.slice4.38').output

    s5 = keras.layers.MaxPooling2D(pool_size=3, strides=1, padding='same',
                                   name='basenet.slice5.0')(s4)
    s5 = keras.layers.Conv2D(1024,
                             kernel_size=(3, 3),
                             padding='same',
                             strides=1,
                             dilation_rate=6,
                             name='basenet.slice5.1')(s5)
    s5 = keras.layers.Conv2D(1024,
                             kernel_size=1,
                             strides=1,
                             padding='same',
                             name='basenet.slice5.2')(s5)

    y = keras.layers.Concatenate()([s5, s4])
    y = upconv(y, n=1, filters=512)
    y = UpsampleLike()([y, s3])
    y = keras.layers.Concatenate()([y, s3])
    y = upconv(y, n=2, filters=256)
    y = UpsampleLike()([y, s2])
    y = keras.layers.Concatenate()([y, s2])
    y = upconv(y, n=3, filters=128)
    y = UpsampleLike()([y, s1])
    y = keras.layers.Concatenate()([y, s1])
    features = upconv(y, n=4, filters=64)

    y = keras.layers.Conv2D(filters=32, kernel_size=3, strides=1, padding='same',
                            name='conv_cls.0')(features)
    y = keras.layers.Activation('relu', name='conv_cls.1')(y)
    y = keras.layers.Conv2D(filters=32, kernel_size=3, strides=1, padding='same',
                            name='conv_cls.2')(y)
    y = keras.layers.Activation('relu', name='conv_cls.3')(y)
    y = keras.layers.Conv2D(filters=16, kernel_size=3, strides=1, padding='same',
                            name='conv_cls.4')(y)
    y = keras.layers.Activation('relu', name='conv_cls.5')(y)
    y = keras.layers.Conv2D(filters=16, kernel_size=1, strides=1, padding='same',
                            name='conv_cls.6')(y)
    y = keras.layers.Activation('relu', name='conv_cls.7')(y)
    y = keras.layers.Conv2D(filters=2, kernel_size=1, strides=1, padding='same',
                            name='conv_cls.8')(y)

    model = keras.models.Model(inputs=inputs, outputs=y)
    if weights_path is not None:
        if weights_path.endswith('.h5'):
            model.load_weights(weights_path)
        elif weights_path.endswith('.pth'):
            load_torch_weights(model=model, weights_path=weights_path)
        else:
            raise NotImplementedError(f'Cannot load weights from {weights_path}')
    return model


def load_torch_weights(model, weights_path):
    import torch

    pretrained = torch.load(weights_path, map_location=torch.device('cpu'))
    layer_names = list(
        set([
            '.'.join(k.split('.')[1:-1]) for k in pretrained.keys()
            if k.split('.')[-1] != 'num_batches_tracked'
        ]))
    for layer_name in layer_names:
        try:
            layer = model.get_layer(layer_name)
        except Exception:  # pylint: disable=broad-except
            print('Skipping', layer.name)
            continue
        if isinstance(layer, keras.layers.BatchNormalization):
            gamma, beta, running_mean, running_std = [
                pretrained[k].numpy() for k in [
                    f'module.{layer_name}.weight',
                    f'module.{layer_name}.bias',
                    f'module.{layer_name}.running_mean',
                    f'module.{layer_name}.running_var',
                ]
            ]
            layer.set_weights([gamma, beta, running_mean, running_std])
        elif isinstance(layer, keras.layers.Conv2D):
            weights, bias = [
                pretrained[k].numpy()
                for k in [f'module.{layer_name}.weight', f'module.{layer_name}.bias']
            ]
            layer.set_weights([weights.transpose(2, 3, 1, 0), bias])

        else:
            raise NotImplementedError

    for layer in model.layers:
        if isinstance(layer, keras.layers.BatchNormalization) or isinstance(
                layer, keras.layers.Conv2D):
            assert layer.name in layer_names


def build_torch_model(weights_path=None):
    from collections import namedtuple, OrderedDict

    import torch
    import torch.nn as nn
    import torch.nn.init as init
    import torch.nn.functional as F
    from torchvision import models

    def init_weights(modules):
        for m in modules:
            if isinstance(m, nn.Conv2d):
                init.xavier_uniform_(m.weight.data)
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                m.weight.data.normal_(0, 0.01)
                m.bias.data.zero_()

    class vgg16_bn(torch.nn.Module):
        def __init__(self, pretrained=True, freeze=True):
            super(vgg16_bn, self).__init__()
            # We don't bother loading the pretrained VGG
            # because we're going to use the weights
            # at weights_path.
            vgg_pretrained_features = models.vgg16_bn(pretrained=False).features
            self.slice1 = torch.nn.Sequential()
            self.slice2 = torch.nn.Sequential()
            self.slice3 = torch.nn.Sequential()
            self.slice4 = torch.nn.Sequential()
            self.slice5 = torch.nn.Sequential()
            for x in range(12):  # conv2_2
                self.slice1.add_module(str(x), vgg_pretrained_features[x])
            for x in range(12, 19):  # conv3_3
                self.slice2.add_module(str(x), vgg_pretrained_features[x])
            for x in range(19, 29):  # conv4_3
                self.slice3.add_module(str(x), vgg_pretrained_features[x])
            for x in range(29, 39):  # conv5_3
                self.slice4.add_module(str(x), vgg_pretrained_features[x])

            # fc6, fc7 without atrous conv
            self.slice5 = torch.nn.Sequential(
                nn.MaxPool2d(kernel_size=3, stride=1, padding=1),
                nn.Conv2d(512, 1024, kernel_size=3, padding=6, dilation=6),
                nn.Conv2d(1024, 1024, kernel_size=1))

            if not pretrained:
                init_weights(self.slice1.modules())
                init_weights(self.slice2.modules())
                init_weights(self.slice3.modules())
                init_weights(self.slice4.modules())

            init_weights(self.slice5.modules())  # no pretrained model for fc6 and fc7

            if freeze:
                for param in self.slice1.parameters():  # only first conv
                    param.requires_grad = False

        def forward(self, X):  # pylint: disable=arguments-differ
            h = self.slice1(X)
            h_relu2_2 = h
            h = self.slice2(h)
            h_relu3_2 = h
            h = self.slice3(h)
            h_relu4_3 = h
            h = self.slice4(h)
            h_relu5_3 = h
            h = self.slice5(h)
            h_fc7 = h
            vgg_outputs = namedtuple("VggOutputs",
                                     ['fc7', 'relu5_3', 'relu4_3', 'relu3_2', 'relu2_2'])
            out = vgg_outputs(h_fc7, h_relu5_3, h_relu4_3, h_relu3_2, h_relu2_2)
            return out

    class double_conv(nn.Module):
        def __init__(self, in_ch, mid_ch, out_ch):
            super(double_conv, self).__init__()
            self.conv = nn.Sequential(nn.Conv2d(in_ch + mid_ch, mid_ch, kernel_size=1),
                                      nn.BatchNorm2d(mid_ch), nn.ReLU(inplace=True),
                                      nn.Conv2d(mid_ch, out_ch, kernel_size=3, padding=1),
                                      nn.BatchNorm2d(out_ch), nn.ReLU(inplace=True))

        def forward(self, x):  # pylint: disable=arguments-differ
            x = self.conv(x)
            return x

    class CRAFT(nn.Module):
        def __init__(self, pretrained=False, freeze=False):
            super(CRAFT, self).__init__()
            # Base network
            self.basenet = vgg16_bn(pretrained, freeze)
            # U network
            self.upconv1 = double_conv(1024, 512, 256)
            self.upconv2 = double_conv(512, 256, 128)
            self.upconv3 = double_conv(256, 128, 64)
            self.upconv4 = double_conv(128, 64, 32)

            num_class = 2
            self.conv_cls = nn.Sequential(
                nn.Conv2d(32, 32, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(32, 32, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(32, 16, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(16, 16, kernel_size=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(16, num_class, kernel_size=1),
            )

            init_weights(self.upconv1.modules())
            init_weights(self.upconv2.modules())
            init_weights(self.upconv3.modules())
            init_weights(self.upconv4.modules())
            init_weights(self.conv_cls.modules())

        def forward(self, x):  # pylint: disable=arguments-differ
            # Base network
            sources = self.basenet(x)
            # U network
            # pylint: disable=E1101
            y = torch.cat([sources[0], sources[1]], dim=1)

            y = self.upconv1(y)

            y = F.interpolate(y, size=sources[2].size()[2:], mode='bilinear', align_corners=False)
            y = torch.cat([y, sources[2]], dim=1)
            y = self.upconv2(y)

            y = F.interpolate(y, size=sources[3].size()[2:], mode='bilinear', align_corners=False)
            y = torch.cat([y, sources[3]], dim=1)
            y = self.upconv3(y)

            y = F.interpolate(y, size=sources[4].size()[2:], mode='bilinear', align_corners=False)
            y = torch.cat([y, sources[4]], dim=1)
            # pylint: enable=E1101
            feature = self.upconv4(y)

            y = self.conv_cls(feature)

            return y.permute(0, 2, 3, 1), feature

    def copyStateDict(state_dict):
        if list(state_dict.keys())[0].startswith("module"):
            start_idx = 1
        else:
            start_idx = 0
        new_state_dict = OrderedDict()
        for k, v in state_dict.items():
            name = ".".join(k.split(".")[start_idx:])
            new_state_dict[name] = v
        return new_state_dict

    model = CRAFT(pretrained=True).eval()
    if weights_path is not None:
        model.load_state_dict(
            copyStateDict(torch.load(weights_path, map_location=torch.device('cpu'))))
    return model


class Detector:
    def __init__(self, pretrained=True, load_from_torch=False):
        if pretrained and load_from_torch:
            weights_path = os.path.expanduser(os.path.join('~', '.keras-ocr', 'craft_mlt_25k.pth'))
            tools.download_and_verify(
                url='https://storage.googleapis.com/keras-ocr/craft_mlt_25k.pth',
                filepath=weights_path,
                sha256='4a5efbfb48b4081100544e75e1e2b57f8de3d84f213004b14b85fd4b3748db17')
        elif pretrained and not load_from_torch:
            weights_path = os.path.expanduser(os.path.join('~', '.keras-ocr', 'craft_mlt_25k.h5'))
            tools.download_and_verify(
                url='https://storage.googleapis.com/keras-ocr/craft_mlt_25k.h5',
                filepath=weights_path,
                sha256='7283ce2ff05a0617e9740c316175ff3bacdd7215dbdf1a726890d5099431f899')
        else:
            weights_path = None
        self.model = build_keras_model(weights_path=weights_path)

    def get_batch_generator(self, image_generator, batch_size=8):
        heatmap = get_gaussian_heatmap(512, 1.5)
        while True:
            batch = [next(image_generator) for n in range(batch_size)]
            images = np.array([entry[0] for entry in batch])
            line_groups = [entry[2] for entry in batch]
            X = compute_input(images)
            # pylint: disable=unsubscriptable-object
            y = np.array([
                compute_maps(heatmap=heatmap,
                             image_height=images.shape[1],
                             image_width=images.shape[2],
                             lines=lines) for lines in line_groups
            ])
            # pylint: enable=unsubscriptable-object
            yield X, y

    def detect(self, images: typing.List[typing.Union[np.ndarray, str]]):
        """Recognize the text in a set of images.

        Args:
            images: Can be a list of numpy arrays of shape HxWx3 or a list of
                filepaths.
        """
        images = [
            compute_input(tools.read(image) if isinstance(image, str) else image)
            for image in images
        ]
        boxes = []
        for image in images:
            boxes.append(getBoxes(self.model.predict(image[np.newaxis]))[0])
        return boxes