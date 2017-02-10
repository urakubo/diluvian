# -*- coding: utf-8 -*-


import itertools
import Queue

import matplotlib.animation as animation
import matplotlib.pyplot as plt
import numpy as np
from tqdm import tqdm

from .config import CONFIG
from .util import get_color_shader, pad_dims, WrappedViewer


class DenseRegion(object):
    def __init__(self, image, target=None, seed_pos=None, mask=None):
        self.MOVE_DELTA = (CONFIG.model.block_size - 1) / 4
        self.queue = Queue.PriorityQueue()
        self.visited = set()
        self.image = image
        self.bounds = image.shape
        self.move_bounds = self.vox_to_pos(self.bounds) - 1
        if mask is None:
            self.mask = np.empty(self.bounds, dtype='float32')
            self.mask[:] = np.NAN
        else:
            self.mask = mask
        self.target = target
        self.bias_against_merge = False
        self.move_based_on_new_mask = False
        if seed_pos is None:
            seed_pos = np.floor_divide(self.move_bounds, 2) + 1
        self.seed_pos = seed_pos
        self.queue.put((None, seed_pos))
        seed_vox = self.pos_to_vox(seed_pos)
        self.mask[tuple(seed_vox)] = CONFIG.model.v_true

    def unfilled_copy(self):
        copy = DenseRegion(self.image, self.target, self.seed_pos)
        copy.bias_against_merge = self.bias_against_merge
        copy.move_based_on_new_mask = self.move_based_on_new_mask

    def vox_to_pos(self, vox):
        return np.floor_divide(vox, self.MOVE_DELTA)

    def pos_to_vox(self, pos):
        return pos * self.MOVE_DELTA

    def get_moves(self, mask):
        moves = []
        ctr = (np.asarray(mask.shape) - 1) / 2 + 1
        for move in map(np.array, [(1, 0, 0), (-1, 0, 0),
                     (0, 1, 0), (0, -1, 0),
                     (0, 0, 1), (0, 0, -1)]):
            plane_min = ctr - (-2 * np.maximum(move, 0) + 1) * self.MOVE_DELTA
            plane_max = ctr + (+2 * np.minimum(move, 0) + 1) * self.MOVE_DELTA + 1
            moves.append({'move': move,
                          'v': mask[plane_min[0]:plane_max[0],
                                    plane_min[1]:plane_max[1],
                                    plane_min[2]:plane_max[2]].max()})
        return moves

    def add_mask(self, mask_block, mask_pos):
        mask_origin = self.pos_to_vox(mask_pos) - (np.asarray(mask_block.shape) - 1) / 2
        current_mask = self.mask[mask_origin[0]:mask_origin[0] + mask_block.shape[0],
                                 mask_origin[1]:mask_origin[1] + mask_block.shape[1],
                                 mask_origin[2]:mask_origin[2] + mask_block.shape[2]]

        if self.bias_against_merge:
            update_mask = np.isnan(current_mask) | (current_mask > 0.5) | np.less(mask_block, current_mask)
            current_mask[update_mask] = mask_block[update_mask]
        else:
            current_mask[:] = mask_block

        self.mask[mask_origin[0]:mask_origin[0] + mask_block.shape[0],
                 mask_origin[1]:mask_origin[1] + mask_block.shape[1],
                 mask_origin[2]:mask_origin[2] + mask_block.shape[2]] = current_mask

        if self.move_based_on_new_mask:
            new_moves = self.get_moves(mask_block)
        else:
            new_moves = self.get_moves(current_mask)
        for move in new_moves:
            new_ctr = mask_pos + move['move']
            if np.any(np.greater_equal(new_ctr, self.move_bounds)) or np.any(new_ctr <= 1):
               continue
            if tuple(new_ctr) not in self.visited and move['v'] >= CONFIG.model.t_move:
                self.visited.add(tuple(new_ctr))
                self.queue.put((-move['v'], tuple(new_ctr)))

    def get_next_block(self):
        next_pos = np.asarray(self.queue.get()[1])
        next_vox = self.pos_to_vox(next_pos)
        margin = (CONFIG.model.block_size - 1) / 2
        block_min = next_vox - margin
        block_max = next_vox + margin + 1
        image_block = self.image[block_min[0]:block_max[0],
                                 block_min[1]:block_max[1],
                                 block_min[2]:block_max[2]]
        mask_block = self.mask[block_min[0]:block_max[0],
                               block_min[1]:block_max[1],
                               block_min[2]:block_max[2]].copy()

        mask_block[np.isnan(mask_block)] = CONFIG.model.v_false

        if self.target is not None:
            target_block = self.target[block_min[0]:block_max[0],
                                       block_min[1]:block_max[1],
                                       block_min[2]:block_max[2]]
        else:
            target_block = None

        assert image_block.shape == tuple(CONFIG.model.block_size), 'Image wrong size: {}'.format(image_block.shape)
        assert mask_block.shape == tuple(CONFIG.model.block_size), 'Mask wrong size: {}'.format(mask_block.shape)
        return {'image': image_block,
                'mask': mask_block,
                'target': target_block,
                'position': next_pos}

    def fill(self, model, verbose=False, move_batch_size=1, max_moves=None, multi_gpu_pad_kludge=None):
        moves = 0
        if verbose:
            pbar = tqdm(desc='Move queue')
        while not self.queue.empty():
            batch_block_data = [self.get_next_block() for _ in itertools.takewhile(lambda _: not self.queue.empty(), range(move_batch_size))]
            batch_moves = len(batch_block_data)
            if verbose:
                moves += batch_moves
                pbar.total = moves + self.queue.qsize()
                pbar.set_description('Move ' + str(batch_block_data[-1]['position']))
                pbar.update(batch_moves)

            image_input = np.concatenate([pad_dims(b['image']) for b in batch_block_data])
            mask_input = np.concatenate([pad_dims(b['mask']) for b in batch_block_data])

            # For models generated with make_parallel that saved the parallel
            # model, not the original model, some kludge is necessary so that
            # the batch size is large enough to give each GPU in the parallel
            # model an equal number of samples.
            if multi_gpu_pad_kludge is not None and image_input.shape[0] % multi_gpu_pad_kludge != 0:
                missing_samples = multi_gpu_pad_kludge - (image_input.shape[0] % multi_gpu_pad_kludge)
                fill_dim = list(image_input.shape)
                fill_dim[0] = missing_samples
                image_input = np.concatenate((image_input, np.zeros(fill_dim, dtype=image_input.dtype)))
                mask_input = np.concatenate((mask_input, np.zeros(fill_dim, dtype=mask_input.dtype)))

            output = model.predict({'image_input': image_input,
                                    'mask_input': mask_input})

            for ind, block_data in enumerate(batch_block_data):
                self.add_mask(output[ind, :, :, :, 0], block_data['position'])

            if max_moves is not None and moves > max_moves:
                break

        if verbose:
            pbar.close()

    def fill_animation(self, model, movie_filename, verbose=False):
        dpi = 100
        fig = plt.figure(figsize=(1920/dpi, 1080/dpi), dpi=dpi)
        fig.patch.set_facecolor('black')
        axes = {
            'xy': fig.add_subplot(1, 3, 1),
            'xz': fig.add_subplot(1, 3, 2),
            'zy': fig.add_subplot(1, 3, 3),
        }

        def get_plane(arr, vox, plane):
            return {
                'xy': lambda a, v: np.transpose(a[:, :, v[2]]),
                'xz': lambda a, v: np.transpose(a[:, v[1], :]),
                'zy': lambda a, v: a[v[0], :, :],
            }[plane](arr, np.round(vox).astype('int64'))

        def get_hv(vox, plane):
            # rel = np.divide(vox, self.bounds)
            rel = vox
            # rel = self.bounds - vox
            return {
                'xy': {'h': rel[1], 'v': rel[0]},
                'xz': {'h': rel[2], 'v': rel[0]},
                'zy': {'h': rel[1], 'v': rel[2]},
            }[plane]

        def get_aspect(plane):
            return {
                'xy': float(CONFIG.volume.resolution[1]) / float(CONFIG.volume.resolution[0]),
                'xz': float(CONFIG.volume.resolution[2]) / float(CONFIG.volume.resolution[0]),
                'zy': float(CONFIG.volume.resolution[1]) / float(CONFIG.volume.resolution[2]),
            }[plane]

        images = {
            'image': {},
            'mask': {},
        }
        lines = {
            'v': {},
            'h': {},
            'bl': {},
            'bt': {},
        }
        current_vox = self.pos_to_vox(self.seed_pos)
        margin = (CONFIG.model.block_size) / 2
        for plane, ax in axes.iteritems():
            ax.get_xaxis().set_visible(False)
            ax.get_yaxis().set_visible(False)

            image_data = get_plane(self.image, current_vox, plane)
            im = ax.imshow(image_data, cmap='gray')
            im.set_clim([0, 1])
            images['image'][plane] = im

            mask_data = get_plane(self.mask, current_vox, plane)
            im = ax.imshow(mask_data, cmap='jet', alpha=0.8)
            im.set_clim([0, 1])
            images['mask'][plane] = im

            aspect = get_aspect(plane)
            lines['h'][plane] = ax.axhline(y=get_hv(current_vox - margin, plane)['h'], color='w')
            lines['v'][plane] = ax.axvline(x=get_hv(current_vox + margin, plane)['v'], color='w')
            lines['bl'][plane] = ax.axvline(x=get_hv(current_vox - margin, plane)['v'], color='w')
            lines['bt'][plane] = ax.axhline(y=get_hv(current_vox + margin, plane)['h'], color='w')

            ax.set_aspect(aspect)

        plt.tight_layout()

        def update_fn(vox):
            if np.array_equal(np.round(vox).astype('int64'), update_fn.next_pos_vox):
                if update_fn.block_data is not None:
                    image_input = pad_dims(update_fn.block_data['image'])
                    mask_input = pad_dims(update_fn.block_data['mask'])

                    output = model.predict({'image_input': image_input,
                                            'mask_input': mask_input})
                    self.add_mask(output[0, :, :, :, 0], update_fn.block_data['position'])

                if not self.queue.empty():
                    update_fn.moves += 1
                    if verbose:
                        update_fn.pbar.total = update_fn.moves + self.queue.qsize()
                        update_fn.pbar.update()
                    update_fn.block_data = self.get_next_block()

                    update_fn.next_pos_vox = self.pos_to_vox(update_fn.block_data['position'])
                    if not np.array_equal(np.round(vox).astype('int64'), update_fn.next_pos_vox):
                        p = update_fn.next_pos_vox - vox
                        steps = np.linspace(0, 1, 16)
                        interp_vox = vox + np.outer(steps, p)
                        for row in interp_vox:
                            update_fn.vox_queue.put(row)
                    else:
                        update_fn.vox_queue.put(vox)

            for plane, im in images['image'].iteritems():
                image_data = get_plane(self.image, vox, plane)
                im.set_data(image_data)

            for plane, im in images['mask'].iteritems():
                image_data = get_plane(self.mask, vox, plane)
                masked_data = np.ma.masked_where(image_data < 0.5, image_data)
                im.set_data(masked_data)

            for plane, ax in axes.iteritems():
                aspect = get_aspect(plane)
                lines['h'][plane].set_ydata(get_hv(vox - margin, plane)['h'])
                lines['v'][plane].set_xdata(get_hv(vox + margin, plane)['v'])
                lines['bl'][plane].set_xdata(get_hv(vox - margin, plane)['v'])
                lines['bt'][plane].set_ydata(get_hv(vox + margin, plane)['h'])

            return images['image'].values() + images['mask'].values() + \
                   lines['h'].values() + lines['v'].values() + \
                   lines['bl'].values() + lines['bt'].values()

        update_fn.moves = 0
        update_fn.block_data = None
        if verbose:
            update_fn.pbar = tqdm(desc='Move queue')

        update_fn.next_pos_vox = current_vox
        update_fn.vox_queue = Queue.Queue()
        update_fn.vox_queue.put(current_vox)

        def vox_gen():
            # count = 0
            # limit = 1000
            last_vox = None
            while 1:
                # print 'gen {}'.format(update_fn.vox_queue.qsize())
                if update_fn.vox_queue.empty():
                    # print 'Done animating'
                    return
                else:
                # count += 1
                    last_vox = update_fn.vox_queue.get()
                    yield last_vox

        ani = animation.FuncAnimation(fig, update_fn, frames=vox_gen(), interval=16, repeat=False, save_count=60*60)
        writer = animation.writers['ffmpeg'](fps=60)

        ani.save(movie_filename, writer=writer, dpi=dpi, savefig_kwargs={'facecolor': 'black'})

        if verbose:
            update_fn.pbar.close()

        return ani

    def get_viewer(self, transpose=False):
        if transpose:
            viewer = WrappedViewer(voxel_size=list(CONFIG.volume.resolution),
                                   voxel_coordinates=self.pos_to_vox(self.seed_pos))
            viewer.add(np.transpose(self.image),
                       name='Image')
            viewer.add(np.transpose(self.target),
                       name='Mask Target',
                       shader=get_color_shader(0))
            viewer.add(np.transpose(self.mask),
                       name='Mask Output',
                       shader=get_color_shader(1))
        else:
            viewer = WrappedViewer(voxel_size=list(np.flipud(CONFIG.volume.resolution)),
                                   voxel_coordinates=np.flipud(self.pos_to_vox(self.seed_pos)))
            viewer.add(self.image,
                       name='Image')
            viewer.add(self.target,
                       name='Mask Target',
                       shader=get_color_shader(0))
            viewer.add(self.mask,
                       name='Mask Output',
                       shader=get_color_shader(1))
        return viewer