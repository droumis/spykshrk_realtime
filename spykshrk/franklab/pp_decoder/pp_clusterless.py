import functools
import logging

import dask
import dask.dataframe as dd
import numpy as np
import pandas as pd
import scipy as sp
import scipy.signal

from spykshrk.franklab.data_containers import LinearPosition, SpikeObservation, EncodeSettings, \
    DecodeSettings, Posteriors, pos_col_format
from spykshrk.franklab.pp_decoder.util import gaussian, normal2D, apply_no_anim_boundary, normal_pdf_int_lookup
from spykshrk.util import Groupby

logger = logging.getLogger(__name__)

class OfflinePPEncoder(object):

    def __init__(self, linflat, spk_amp, speed_thresh, encode_settings: EncodeSettings,
                 dask_worker_memory=None, dask_memory_utilization=0.5, dask_chunksize=None):

        if dask_worker_memory is None and dask_chunksize is None:
            raise TypeError('OfflinePPEncoder requires either dask_memory or dask_chunksize to be set.')
        if dask_worker_memory is not None and dask_chunksize is not None:
            raise TypeError('OfflinePPEncoder only allows one to be set, dask_memory or dask_chunksize.')
        if dask_chunksize is not None:
            memory_per_dec = (len(spk_amp) * np.sum([np.dtype(dtype).itemsize for dtype in spk_amp.dtypes]))
            self.dask_chunksize = dask_chunksize
            logger.info('Manual Dask chunksize: {}'.format(self.dask_chunksize))
            logger.info('Expected worker peak memory usage: {:0.2f} MB'.format(self.dask_chunksize * memory_per_dec / 2**20))
            logger.info('Worker total memory: UNKNOWN')

        if dask_worker_memory is not None:
            memory_per_dec = (len(spk_amp) * np.sum([np.dtype(dtype).itemsize for dtype in spk_amp.dtypes]))
            self.dask_chunksize = np.int(dask_memory_utilization * dask_worker_memory / memory_per_dec)
            logger.info('Dask chunksize: {}'.format(self.dask_chunksize))
            logger.info('Memory utilization at: {:0.1f}%'.format(dask_memory_utilization * 100))
            logger.info('Expected worker peak memory usage: {:0.2f} MB'.
                        format(self.dask_chunksize * memory_per_dec / 2**20))

        self.linflat = linflat
        self.spk_amp = spk_amp
        self.speed_thresh = speed_thresh
        self.encode_settings = encode_settings
        self.calc_occupancy()


    def calc_occupancy(self):
        occ, _ = np.histogram(a=self.linflat['linpos_flat'], bins=self.encode_settings.pos_bin_edges, normed=True)
        self.occupancy = np.convolve(occ, self.encode_settings.pos_kernel)[int(len(occ)/2):int(len(occ)*3/2)]
        return self.occupancy

    def run_encoder(self):
        logging.info("Setting up encoder dask task.")
        task = self.setup_encoder_dask()
        logging.info("Running compute tasks on dask workers.")
        results = dask.compute(*task)
        return results

    def setup_encoder_dask(self):

        grp = self.spk_amp.groupby('elec_grp_id')
        observations = {}
        task = []
        for tet_id, spk_tet in grp:
            spk_tet.index = spk_tet.index.droplevel('elec_grp_id')
            tet_lin_pos = (self.linflat.get_irregular_resampled(spk_tet.index.get_level_values('timestamp'))
                           .get_mapped_single_axis())
            # tet_lin_pos = self.linflat.iloc[
            #     self.linflat.index.get_level_values('timestamp').
            #     get_indexer_for(spk_tet.index.get_level_values('timestamp'), method='nearest')
            #     ]
            # Velocity threshold on spikes and position
            tet_lin_pos_thresh = tet_lin_pos.get_above_velocity(self.speed_thresh)
            spk_tet_thresh = spk_tet.set_index(tet_lin_pos_thresh.index)
            # Decode from all spikes
            dask_spk_tet = dd.from_pandas(spk_tet.get_simple_index(), chunksize=self.dask_chunksize)

            df_meta = pd.DataFrame([], columns=[pos_col_format(ii, self.encode_settings.pos_num_bins)
                                                for ii in range(self.encode_settings.pos_num_bins)])

            # Setup decode of all spikes from encoding of velocity threshold spikes
            task.append(dask_spk_tet.map_partitions(functools.partial(self.compute_observ_tet, enc_spk=spk_tet_thresh,
                                                                      tet_lin_pos=tet_lin_pos_thresh,
                                                                      occupancy=self.occupancy,
                                                                      encode_settings=self.encode_settings),
                                                    meta=df_meta))

        return task

    def compute_observ_tet(self, dec_spk, enc_spk, tet_lin_pos, occupancy, encode_settings):

        pos_distrib_tet = sp.stats.norm.pdf(np.expand_dims(encode_settings.pos_bins, 0),
                                            np.expand_dims(tet_lin_pos['linpos_flat'], 1),
                                            encode_settings.pos_kernel_std)

        mark_contrib = normal_pdf_int_lookup(np.expand_dims(dec_spk, 1),
                                             np.expand_dims(enc_spk, 0),
                                             encode_settings.mark_kernel_std)

        all_contrib = np.prod(mark_contrib, axis=2)

        del mark_contrib

        observ = np.matmul(all_contrib, pos_distrib_tet)

        del all_contrib

        # occupancy normalize
        observ = observ / (occupancy + 1e-10)

        # normalize each row
        observ_sum = observ.sum(axis=1)
        observ_sum_zero = observ_sum == 0
        observ[observ_sum_zero, :] = 1/(self.encode_settings.pos_bins[-1] - self.encode_settings.pos_bins[0])
        observ_sum[observ_sum_zero] = 1
        observ = observ / observ.sum(axis=1)[:, np.newaxis]

        ret_df = pd.DataFrame(observ, index=dec_spk.index,
                              columns=[pos_col_format(pos_ii, observ.shape[1])
                                       for pos_ii in range(observ.shape[1])])
        return ret_df

class OfflinePPDecoder(object):
    """
    Implementation of Adaptive Marked Point Process Decoder [Deng, et. al. 2015].

    Requires linearized position (spykshrk.franklab.pp_decoder.LinearPositionContainer)
    and spike observation containers (spykshrk.franklab.pp_decoder.SpikeObservation).
    along with encoding (spykshrk.franklab.pp_decoder.EncodeSettings)
    and decoding settings (spykshrk.franklab.pp_decoder.DecodeSettings).

    """
    def __init__(self, lin_obj: LinearPosition, observ_obj: SpikeObservation, encode_settings: EncodeSettings,
                 decode_settings: DecodeSettings, which_trans_mat='learned', time_bin_size=30, parallel=True,
                 dask_worker_memory=None, dask_memory_utilization=0.5, dask_chunksize=None):
        """
        Constructor for OfflinePPDecoder.

        Args:
            lin_obj (LinearPositionContainer): Linear position of animal.
            observ_obj (SpikeObservation): Observered position distribution for each spike.
            encode_settings (EncodeSettings): Realtime encoder settings.
            decode_settings (DecodeSettings): Realtime decoder settings.
            which_trans_mat (str): Which point process transition matrix to use (learned, simple, uniform).
            time_bin_size (float, optional): Delta time per bin to run decode, defaults to decoder_settings value.
        """
        if dask_worker_memory is None and dask_chunksize is None:
            raise TypeError('OfflinePPDecoder requires either dask_memory or dask_chunksize to be set.')
        if dask_worker_memory is not None and dask_chunksize is not None:
            raise TypeError('OfflinePPDecoder only allows one to be set, dask_memory or dask_chunksize.')
        if dask_chunksize is not None:
            self.dask_chunksize = dask_chunksize
        if dask_worker_memory is not None:
            memory_per_dec = (len(spk_amp) * np.sum([np.dtype(dtype).itemsize for dtype in spk_amp.dtypes]))
            self.dask_chunksize = np.int(dask_memory_utilization * dask_worker_memory / memory_per_dec)
            logger.info('Dask chunksize: {}'.format(self.dask_chunksize))
            logger.info('Memory utilization at: {:0.1f}%'.format(dask_memory_utilization * 100))
            logger.info('Expected worker memory usage: {:0.2f} MB'.format(self.dask_chunksize * memory_per_dec / 2**20))

        self.lin_obj = lin_obj
        self.observ_obj = observ_obj
        self.observ_obj = observ_obj
        self.encode_settings = encode_settings
        self.decode_settings = decode_settings
        self.which_trans_mat = which_trans_mat
        self.time_bin_size = time_bin_size

        if self.which_trans_mat == 'learned':
            self.trans_mat = self.calc_learned_state_trans_mat(self.lin_obj.get_mapped_single_axis(),
                                                               self.encode_settings, self.decode_settings)
        elif self.which_trans_mat == 'simple':
            self.trans_mat = self.calc_simple_trans_mat(self.encode_settings)
        elif self.which_trans_mat == 'uniform':
            self.trans_mat = self.calc_uniform_trans_mat(self.encode_settings)

        self.parallel = parallel

        self.firing_rate = None
        self.occupancy = None
        self.prob_no_spike = None
        self.likelihoods = None
        self.posteriors = None
        self.posteriors_obj = None

    def __del__(self):
        print('decoder deleting')

    def run_decoder(self):
        """
        Run the decoder at a given time bin size.  Intermediate results are saved as
        attributes to the class.

        Args:
            time_bin_size (float, optional): Delta time per bin.

        Returns (pd.DataFrame): Final decoded posteriors that estimate position.

        """

        self.recalc_firing_rate()
        self.recalc_occupancy()
        self.recalc_prob_no_spike()
        print("Beginning likelihood calculation")
        self.recalc_likelihood()
        print("Beginning posterior calculation")
        self.recalc_posterior()

        return self.posteriors_obj

    def recalc_firing_rate(self):
        self.firing_rate = self._calc_firing_rate_tet(self.observ_obj, self.encode_settings)

    def recalc_occupancy(self):
        self.occupancy = self.calc_occupancy(self.lin_obj, self.encode_settings)

    def recalc_prob_no_spike(self):
        self.prob_no_spike = self.calc_prob_no_spike(self.firing_rate, self.occupancy, self.encode_settings,
                                                     self.decode_settings)

    def recalc_likelihood(self):
        self.likelihoods = self.calc_observation_intensity(self.observ_obj,
                                                           self.prob_no_spike,
                                                           self.encode_settings,
                                                           self.decode_settings,
                                                           time_bin_size=self.time_bin_size,
                                                           chunksize=self.dask_chunksize)

    def recalc_posterior(self):
        self.posteriors = self.calc_posterior(self.likelihoods, self.trans_mat, self.encode_settings)
        self.posteriors_obj = Posteriors.from_dataframe(self.posteriors, enc_settings=self.encode_settings,
                                                        dec_settings=self.decode_settings)

    @staticmethod
    def calc_learned_state_trans_mat(linpos_simple, enc_settings, dec_settings):
        """
        Calculate the point process transition matrix using the real behavior of the animal.
        This is the 2D matrix that defines the possible range of transitions in the position
        estimate.

        The learned values are smoothed with a gaussian kernel and a uniform offset is added,
        specified by the encoding config.  The matrix is column normalized.

        Args:
            linpos_simple (pd.DataFrame): Linear position pandas table with no MultiIndex.
            enc_settings (EncodeSettings): Encoder settings from a realtime config.
            dec_settings (DecodeSettings): Decoder settings from a realtime config.

        Returns: Learned transition matrix.

        """
        pos_num_bins = len(enc_settings.pos_bins)

        # Smoothing kernel for learned pos transition matrix
        xv, yv = np.meshgrid(np.arange(-20, 21), np.arange(-20, 21))
        kernel = normal2D(xv, yv, dec_settings.trans_smooth_std)
        kernel /= kernel.sum()

        linpos_state = linpos_simple
        linpos_ind = np.searchsorted(enc_settings.pos_bins, linpos_state, side='right') - 1

        # Create learned pos transition matrix
        learned_trans_mat = np.zeros([pos_num_bins, pos_num_bins])
        for first_pos_ind, second_pos_ind in zip(linpos_ind[:-1], linpos_ind[1:]):
            learned_trans_mat[first_pos_ind, second_pos_ind] += 1

        # normalize
        learned_trans_mat = learned_trans_mat / (learned_trans_mat.sum(axis=0)[None, :])
        learned_trans_mat[np.isnan(learned_trans_mat)] = 0

        # smooth
        learned_trans_mat = sp.signal.convolve2d(learned_trans_mat, kernel, mode='same')
        learned_trans_mat = apply_no_anim_boundary(enc_settings.pos_bins, enc_settings.arm_coordinates,
                                                   learned_trans_mat)

        # uniform offset
        uniform_gain = dec_settings.trans_uniform_gain
        uniform_dist = np.ones(learned_trans_mat.shape)

        # no-animal boundary
        uniform_dist = apply_no_anim_boundary(enc_settings.pos_bins, enc_settings.arm_coordinates, uniform_dist)

        # normalize uniform offset
        uniform_dist = uniform_dist / (uniform_dist.sum(axis=0)[None, :])
        uniform_dist[np.isnan(uniform_dist)] = 0

        # apply uniform offset
        learned_trans_mat = learned_trans_mat * (1 - uniform_gain) + uniform_dist * uniform_gain

        # renormalize
        learned_trans_mat = learned_trans_mat / (learned_trans_mat.sum(axis=0)[None, :])
        learned_trans_mat[np.isnan(learned_trans_mat)] = 0

        return learned_trans_mat

    @staticmethod
    def calc_simple_trans_mat(enc_settings):
        """
        Calculate a simple point process transition matrix using a gaussian kernel.

        Args:
            enc_settings (EncodeSettings): Encoder setting from realtime config.

        Returns (np.array): Simple gaussian transition matrix.

        """
        pos_num_bins = len(enc_settings.pos_bins)

        # Setup transition matrix
        transition_mat = np.ones([pos_num_bins, pos_num_bins])
        for bin_ii in range(pos_num_bins):
            transition_mat[bin_ii, :] = gaussian(enc_settings.pos_bins, enc_settings.pos_bins[bin_ii], 3)

        # uniform offset
        uniform_gain = 0.01
        uniform_dist = np.ones(transition_mat.shape)

        # normalize transition matrix
        transition_mat = transition_mat/(transition_mat.sum(axis=0)[None, :])

        # normalize uniform offset
        uniform_dist = uniform_dist/(uniform_dist.sum(axis=0)[None, :])

        # apply uniform offset
        transition_mat = transition_mat * (1 - uniform_gain) + uniform_dist * uniform_gain

        return transition_mat

    @staticmethod
    def calc_uniform_trans_mat(enc_settings):
        """
        Calculate a simple uniform point process transition matrix.

        Args:
            enc_settings (EncodeSettings): Encoder setting from realtime config.

        Returns (np.array): Simple uniform transition matrix.

        """

        pos_num_bins = len(enc_settings.pos_bins)

        # Setup transition matrix
        transition_mat = np.ones([pos_num_bins, pos_num_bins])
        transition_mat = apply_no_anim_boundary(enc_settings.pos_bins, enc_settings.arm_coordinates, transition_mat)

        # normalize transition matrix
        transition_mat = transition_mat/(transition_mat.sum(axis=0)[None, :])

        transition_mat = np.nan_to_num(transition_mat)

        return transition_mat

    @staticmethod
    def calc_observation_intensity(observ: SpikeObservation,
                                   prob_no_spike,
                                   enc_settings: EncodeSettings,
                                   dec_settings: DecodeSettings,
                                   time_bin_size=None,
                                   chunksize=30000):
        """

        Args:
            observ (SpikeObservation): Object containing observation data frame, one row per spike observed.
            enc_settings (EncodeSettings): Encoder settings from realtime config.
            dec_settings (DecodeSettings): Decoder settings from realtime config.
            time_bin_size (float): Delta time per bin.

        Returns: (pd.DataFrame, dict[int, np.array]) DataFrame of observation per time bin in each row.
            Dictionary of numpy arrays, one per tetrode, containing occupancy firing rate.

        """
        day = observ.index.get_level_values('day')[0]
        epoch = observ.index.get_level_values('epoch')[0]

        if time_bin_size is not None:
            observ.update_observations_bins(time_bin_size=time_bin_size, inplace=True)
        else:
            time_bin_size = dec_settings.time_bin_size
            observ.update_observations_bins(time_bin_size=time_bin_size, inplace=True)

        observ.update_parallel_bins(60000, inplace=True)

        observ.update_num_missing_future_bins(inplace=True)

        observ_dask = dd.from_pandas(observ.get_no_multi_index(), chunksize=chunksize)
        observ_grp = observ_dask.groupby('parallel_bin')

        observ_meta = [(key, 'f8') for key in [pos_col_format(ii, enc_settings.pos_num_bins)
                                               for ii in range(enc_settings.pos_num_bins)]]
        observ_meta.append(('timestamp', 'f8'))
        observ_meta.append(('num_spikes', 'f8'))
        observ_meta.append(('dec_bin', 'f8'))

        elec_grp_list = observ['elec_grp_id'].unique()
        observ_task = observ_grp.apply(functools.partial(OfflinePPDecoder._calc_observation_single_bin,
                                                         elec_grp_list=elec_grp_list,
                                                         prob_no_spike=prob_no_spike,
                                                         time_bin_size=time_bin_size,
                                                         enc_settings=enc_settings),
                                       meta=observ_meta)

        dec_agg_results = observ_task.compute()
        dec_agg_results.sort_values('timestamp', inplace=True)

        dec_new_ind = pd.MultiIndex.from_product([[day], [epoch], dec_agg_results['timestamp']])
        lev = list(dec_new_ind.levels)
        lab = list(dec_new_ind.labels)
        lev.append(dec_agg_results['timestamp']/float(enc_settings.sampling_rate))
        lab.append(range(len(dec_agg_results)))

        dec_new_ind = pd.MultiIndex(levels=lev, labels=lab, names=['day', 'epoch', 'timestamp', 'time'])

        dec_agg_results.set_index(dec_new_ind, inplace=True)

        binned_observ = dec_agg_results

        #dec_agg_results['day'] = day
        #dec_agg_results['epoch'] = epoch
        #dec_agg_results['time'] = dec_agg_results['timestamp']/float(enc_settings.sampling_rate)
        #binned_observ = dec_agg_results.set_index(['day', 'epoch', 'timestamp', 'time'])

        # Smooth and normalize firing rate (conditional intensity function)

        return binned_observ

    @staticmethod
    def _calc_observation_single_bin(spikes_in_parallel, elec_grp_list,
                                     prob_no_spike, time_bin_size, enc_settings):

        global_prob_no_spike = np.prod(list(prob_no_spike.values()), axis=0)

        results = []
        #parallel_id = spikes_in_parallel['parallel_bin'].iloc[0]
        #dec_grp = spikes_in_parallel.groupby('dec_bin')

        dec_grp = Groupby(spikes_in_parallel.values, spikes_in_parallel['dec_bin'].values)

        pos_col_ind = spikes_in_parallel.columns.slice_locs(enc_settings.pos_col_names[0],
                                                            enc_settings.pos_col_names[-1])
        elec_grp_ind = spikes_in_parallel.columns.get_loc('elec_grp_id')
        num_missing_ind = spikes_in_parallel.columns.get_loc('num_missing_bins')
        dec_bin_start_ind = spikes_in_parallel.columns.get_loc('dec_bin_start')

        for dec_bin_ii, spikes_in_bin in dec_grp:
            #print('parallel #{} dec #{}'.format(parallel_id, dec_bin_ii))
            obv_in_bin = np.ones(enc_settings.pos_num_bins)

            num_spikes = len(spikes_in_bin)

            elec_set = set()

            spike_bin_raw = spikes_in_bin

            """obv_in_bin = spike_bin_raw[:, slice(*pos_col_ind)].prod(axis=0)

            for elec_grp_id in spike_bin_raw[:, elec_grp_ind]:
                elec_set.add(elec_grp_id)
                obv_in_bin *= prob_no_spike[elec_grp_id]

            obv_in_bin = obv_in_bin / (np.sum(obv_in_bin) * enc_settings.pos_bin_delta)

            num_missing_bins = spike_bin_raw[-1,num_missing_ind]
            """
            # Contribution of each spike
            for obv, elec_grp_id, num_missing_bins in zip(spike_bin_raw[:, slice(*pos_col_ind)],
                                                          spike_bin_raw[:, elec_grp_ind],
                                                          spike_bin_raw[:, num_missing_ind]):

                elec_set.add(elec_grp_id)

                obv_in_bin = obv_in_bin * obv
                obv_in_bin = obv_in_bin * prob_no_spike[elec_grp_id]
                obv_in_bin = obv_in_bin / (np.sum(obv_in_bin) * enc_settings.pos_bin_delta)

            # Contribution for electrodes that no spikes in this bin
            for elec_grp_id in elec_set.symmetric_difference(elec_grp_list):
                obv_in_bin = obv_in_bin * prob_no_spike[elec_grp_id]

            dec_bin_timestamp = spike_bin_raw[0, dec_bin_start_ind]
            results.append(np.concatenate([obv_in_bin, [dec_bin_timestamp, num_spikes, dec_bin_ii]]))

            for missing_ii in range(int(num_missing_bins)):
                results.append(np.concatenate([global_prob_no_spike, [dec_bin_timestamp+((missing_ii+1)*time_bin_size),
                                                                      0, dec_bin_ii+missing_ii+1]]))

        likelihoods = pd.DataFrame(np.vstack(results),
                                   columns=enc_settings.pos_col_names+['timestamp', 'num_spikes', 'dec_bin'])
        return likelihoods

    @staticmethod
    def _calc_firing_rate_tet(observ: SpikeObservation, enc_settings: EncodeSettings):
        # initialize conditional intensity function
        firing_rate = {}
        tet_pos_groups = observ.loc[:, ('elec_grp_id', 'position')].groupby('elec_grp_id')

        for tet_id, tet_spikes in tet_pos_groups:
            tet_pos_hist, _ = np.histogram(tet_spikes, bins=enc_settings.pos_bin_edges)

            firing_rate[tet_id] = tet_pos_hist


        for fr_key in firing_rate.keys():
            firing_rate[fr_key] = np.convolve(firing_rate[fr_key], enc_settings.pos_kernel, mode='same')

            firing_rate[fr_key] = apply_no_anim_boundary(enc_settings.pos_bins, enc_settings.arm_coordinates,
                                                         firing_rate[fr_key])

            firing_rate[fr_key] = firing_rate[fr_key] / (firing_rate[fr_key].sum() * enc_settings.pos_bin_delta)

        return firing_rate

    @staticmethod
    def calc_occupancy(lin_obj: LinearPosition, enc_settings: EncodeSettings):
        """

        Args:
            lin_obj (LinearPositionContainer): Linear position of the animal.
            enc_settings (EncodeSettings): Realtime encoding settings.

        Returns (np.array): The occupancy of the animal

        """
        occupancy, occ_bin_edges = np.histogram(lin_obj.get_mapped_single_axis(), bins=enc_settings.pos_bin_edges,
                                                normed=True)

        occupancy = np.convolve(occupancy, enc_settings.pos_kernel, mode='same')

        occupancy += 1e10

        return occupancy

    @staticmethod
    def calc_prob_no_spike(firing_rate: dict, occupancy, enc_settings: EncodeSettings, dec_settings: DecodeSettings):
        """

        Args:
            firing_rate (pd.DataFrame): Occupancy firing rate, from calc_observation_intensity(...).
            occupancy (np.array): The occupancy of the animal.
            enc_settings (EncodeSettings): Realtime encode settings.
            dec_settings (DecodeSettings): Realtime decoding settings.

        Returns (dict[int, np.array]): Dictionary of probability that no spike occured per tetrode.

        """
        prob_no_spike = {}
        for tet_id, tet_fr in firing_rate.items():
            prob_no_spike[tet_id] = np.exp(-dec_settings.time_bin_size/enc_settings.sampling_rate * tet_fr / occupancy)

        return prob_no_spike

    @staticmethod
    def calc_posterior(likelihoods, transition_mat, enc_settings: EncodeSettings):
        """

        Args:
            likelihoods (pd.DataFrame): The evaluated likelihood function per time bin, from calc_likelihood(...).
            transition_mat (np.array): The point process state transition matrix.
            enc_settings (EncodeSettings): Realtime encoding settings.

        Returns (pd.DataFrame): The decoded posteriors per time bin estimating the animal's location.

        """
        likelihoods_pos = likelihoods.loc[:, pos_col_format(0, enc_settings.pos_num_bins):
                                          pos_col_format(enc_settings.pos_num_bins-1,
                                                         enc_settings.pos_num_bins)]

        posteriors = OfflinePPDecoder._posterior_from_numpy(likelihoods_pos.values,
                                                            transition_mat, enc_settings.pos_num_bins,
                                                            enc_settings.pos_bin_delta)

        # posteriors = pp_cy.calc_posterior_cy(likelihoods_pos.values.copy(order='C'),
        #                                      transition_mat.copy(order='C'), enc_settings.pos_num_bins,
        #                                      enc_settings.pos_bin_delta)

        # copy observ DataFrame and replace with likelihoods, preserving other columns

        posteriors_df = pd.DataFrame(posteriors, index=likelihoods.index,
                                     columns=enc_settings.pos_col_names)

        posteriors_df['num_spikes'] = likelihoods['num_spikes']
        posteriors_df['dec_bin'] = likelihoods['dec_bin']

        return posteriors_df

    @staticmethod
    def _posterior_from_numpy(likelihoods, transition_mat, pos_num_bins, pos_delta):

        last_posterior = np.ones(pos_num_bins)

        posteriors = np.zeros(likelihoods.shape)

        for like_ii, like in enumerate(likelihoods):
            posteriors[like_ii, :] = like * np.matmul(transition_mat, last_posterior)
            posteriors[like_ii, :] = posteriors[like_ii, :] / (posteriors[like_ii, :].sum() *
                                                               pos_delta)
            last_posterior = posteriors[like_ii, :]

        # copy observ DataFrame and replace with likelihoods, preserving other columns

        return posteriors
