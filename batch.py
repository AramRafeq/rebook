import argparse
import cv2
import glob
import numpy as np
import os
import re
from multiprocessing.pool import Pool
from os.path import join, isfile
from subprocess import check_call

import algorithm
from algorithm import skew_angle, safe_rotate
from binarize import binarize, adaptive_otsu
from lib import debug_imwrite
import lib

class Crop(object):
    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0 = x0, y0
        self.x1, self.y1 = x1, y1

    @property
    def w(self):
        return self.x1 - self.x0

    @property
    def h(self):
        return self.y1 - self.y0

    def nonempty(self):
        return self.x0 <= self.x1 and self.y0 <= self.y1

    def intersect(self, other):
        return Crop(
            max(self.x0, other.x0),
            max(self.y0, other.y0),
            min(self.x1, other.x1),
            min(self.y1, other.y1),
        )

    @classmethod
    def intersect_all(cls, crops):
        return reduce(Crop.intersect, crops)

    def union(self, other):
        return Crop(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    @classmethod
    def union_all(cls, crops):
        return reduce(Crop.union, crops)

    def apply(self, im):
        assert self.nonempty()
        return im[self.y0:self.y1, self.x0:self.x1]

    @classmethod
    def full(cls, im):
        h, w = im.shape
        return Crop(0, 0, w, h)

    @classmethod
    def null(cls, im):
        h, w = im.shape
        return Crop(w, h, 0, 0)

    @classmethod
    def from_rect(cls, x, y, w, h):
        return Crop(x, y, x + w, y + h)

    def __repr__(self):
        return "Crop({}, {}, {}, {})".format(self.x0, self.y0, self.x1, self.y1)

def draw_crop(im, crop, color, thickness=2):
    if not lib.debug: return
    cv2.rectangle(im, (crop.x0, crop.y0), (crop.x1, crop.y1), color, thickness)

def split_crops(crops):
    # Maximize horizontal separation
    # sorted by starting x value, ascending).
    crops = sorted(crops, key=lambda crop: crop.x0)

    # Greedy algorithm. Maximize L bound of R minus R bound of L.
    current_r = 0
    quantity = -100000
    argmax = -1
    for idx, crop in enumerate(crops[:-1]):
        current_r = max(current_r, crop.x1)
        x2 = crops[idx + 1].x0
        # print 'x2:', x2, 'r:', current_r, 'quantity:', x2 - current_r
        if x2 - current_r > quantity:
            quantity = x2 - current_r
            argmax = idx

    print 'split:', argmax, 'out of', len(crops), '@', current_r

    return [l for l in (crops[:argmax + 1], crops[argmax + 1:]) if l]

def draw_box(debug, c, color, thickness):
    if not lib.debug: return
    x, y, w, h = cv2.boundingRect(c)
    cv2.rectangle(debug, (x, y), (x + w, y + h), color, 4)

def crop(im, bw, split=True):
    im_h, im_w = im.shape

    AH = algorithm.dominant_char_height(bw)
    letter_boxes = algorithm.letter_contours(AH, bw)
    lines = algorithm.collate_lines(AH, letter_boxes)

    stroke_widths = algorithm.fast_stroke_width(bw)
    debug_imwrite('strokes.png', lib.normalize_u8(stroke_widths.clip(0, 10)))

    mask = np.zeros(im.shape, dtype=np.uint8)
    letter_contours = [c for (c, _, _, _, _) in letter_boxes]
    cv2.drawContours(mask, letter_contours, -1, 255, thickness=cv2.FILLED)

    masked_strokes = np.ma.masked_where(mask ^ 255, stroke_widths)
    strokes_mean = masked_strokes.mean()
    strokes_std = masked_strokes.std()
    print 'overall: mean:', strokes_mean, 'std:', strokes_std

    debug = cv2.cvtColor(im, cv2.COLOR_GRAY2RGB)
    line_crops = []
    good_contours = []
    for line in lines:
        line_crop = Crop.null(bw)
        if len(line) <= 1: continue
        for c, x, y, w, h in line:
            crop = Crop.from_rect(x, y, w, h)

            mask = np.zeros((h, w), dtype=np.uint8)
            cv2.drawContours(mask, [c], 0, 255,
                             thickness=cv2.FILLED, offset=(-x, -y))
            masked_strokes = np.ma.masked_where(mask ^ 255,
                                                crop.apply(stroke_widths))
            # print 'mean:', masked_strokes.mean(), 'std:', masked_strokes.std()
            mean = masked_strokes.mean()
            if mean < strokes_mean - strokes_std:
                print 'skipping', x, y,
                print '  mean:', masked_strokes.mean(), 'std:', masked_strokes.std()
                draw_box(debug, c, (0, 0, 255), 2)
            else:
                draw_box(debug, c, (0, 255, 0), 2)
                line_crop = line_crop.union(crop)
                good_contours.append(c)

        line_crops.append(line_crop)

    line_lefts = np.array([lc.x0 for lc in line_crops])
    line_rights = np.array([lc.x1 for lc in line_crops])
    line_start_thresh = np.percentile(line_lefts, 30)
    line_end_thresh = np.percentile(line_rights, 70)
    good_line_crops = []
    line_crop_debug = cv2.cvtColor(im, cv2.COLOR_GRAY2RGB)
    for line_crop in line_crops:
        if split == False and \
                (line_crop.x1 + 10 * AH < line_start_thresh or
                 line_crop.x0 - 10 * AH > line_end_thresh):
            draw_crop(line_crop_debug, line_crop, (0, 0, 255))
        else:
            good_line_crops.append(line_crop)
            draw_crop(line_crop_debug, line_crop, (0, 255, 0))

    debug_imwrite("line_debug.png", line_crop_debug)
    debug_imwrite("debug.png", debug)

    line_crops = filter(lambda lc: lc.nonempty() and \
                        not np.all(lc.apply(bw) == 255),
                        good_line_crops)

    if not line_crops:
        return AH, lines, [Crop.full(im)]

    if split and im_w > im_h:  # two pages
        crop_sets = split_crops(line_crops)
    else:
        crop_sets = [line_crops]

    return AH, lines, [Crop.union_all(cs) for cs in crop_sets]

extension = '.tif'
def process_image(original, dpi):
    # original = cv2.resize(original, (0, 0), None, 1.5, 1.5)
    im_h, im_w = original.shape
    # image height should be about 10 inches. round to 100
    if not dpi:
        dpi = int(round(im_h / 1100.0) * 100)
        print 'detected dpi:', dpi
    split = im_w > im_h # two pages

    bw = binarize(original, adaptive_otsu, resize=1.0)
    debug_imwrite('thresholded.png', bw)
    AH, lines, crops = crop(original, bw, split=split)

    outimgs = []
    for idx, c in enumerate(crops):
        if c.nonempty():
            bw_cropped = c.apply(bw)
            orig_cropped = c.apply(original)
            angle = skew_angle(bw_cropped, original, AH, lines)
            rotated = safe_rotate(orig_cropped, angle)

            lib.debug = False
            rotated_bw = binarize(rotated, adaptive_otsu, resize=1.0)
            _, _, [new_crop] = crop(rotated, rotated_bw, split=False)

            if new_crop.nonempty():
                outimgs.append(new_crop.apply(rotated_bw))

    return dpi, outimgs

def process_file((inpath, outdir, dpi)):
    outfiles = glob.glob('{}/{}_*{}'.format(outdir, inpath[:-4], extension))
    if outfiles:
        print 'skipping', inpath
        return outfiles
    else:
        print 'processing', inpath

    original = cv2.imread(inpath, cv2.IMREAD_UNCHANGED)
    dpi, outimgs = process_image(original, dpi)
    for idx, outimg in enumerate(outimgs):
        outfile = '{}/{}_{}{}'.format(outdir, inpath[:-4], idx, extension)
        print '    writing', outfile
        cv2.imwrite(outfile, outimg)
        check_call(['tiffset', '-s', '282', str(dpi), outfile])
        check_call(['tiffset', '-s', '283', str(dpi), outfile])
        outfiles.append(outfile)

    return outfiles

def run(args):
    if args.single_file:
        lib.debug = True
        im = cv2.imread(args.single_file, cv2.IMREAD_UNCHANGED)
        _, outimgs = process_image(im, args.dpi)
        for idx, outimg in enumerate(outimgs):
            cv2.imwrite('out{}.png'.format(idx), outimg)
        return

    paths = [[join(indir, fn) for fn in os.listdir(indir)] for indir in args.indirs]
    files = filter(lambda f: re.search('.(png|jpg|tif)$', f),
                sum(paths, []))
    files.sort(key=lambda f: map(int, re.findall('[0-9]+', f)))
    im = cv2.imread(files[0], cv2.IMREAD_UNCHANGED)

    for d in args.indirs:
        if not os.path.isdir(join(args.outdir, d)):
            os.makedirs(join(args.outdir, d))

    if args.concurrent:
        pool = Pool(2)
        outfiles = pool.map(process_file, zip(files,
                            [args.outdir] * len(files),
                            [args.dpi] * len(files)))
    else:
        outfiles = map(process_file, zip(files,
                       [args.outdir] * len(files),
                       [args.dpi] * len(files)))

    outfiles = sum(outfiles, [])
    outfiles.sort(key=lambda f: map(int, re.findall('[0-9]+', f)))

    outtif = join(args.outdir, 'out.tif')
    outpdf = join(args.outdir, 'out.pdf')
    if not isfile(outpdf):
        if not isfile(outtif):
            print 'making tif:', outtif
            check_call(['tiffcp'] + outfiles + [outtif])

        print 'making pdf:', outpdf
        check_call([
            'tiff2pdf', '-q', '100', '-j', '-p', 'letter',
            '-o', outpdf, outtif
        ])

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Batch-process for PDF')
    parser.add_argument('outdir', nargs='?', help="Output directory")
    parser.add_argument('indirs', nargs='+', help="Input directory")
    parser.add_argument('-f', '--file', dest='single_file', action='store',
                        help="Run on single file instead")
    parser.add_argument('-c', '--concurrent', action='store_true',
                        help="Run w/ threads.")
    parser.add_argument('-d', '--dpi', action='store', type=int,
                        help="Force a particular DPI")

    run(parser.parse_args())
