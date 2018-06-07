



def decode_new_spikes(encoding_model:OfflinePPEncoder, spikes_to_decode:SpikeFeatures,
 decode_settings:DecodeSettings):

    """
    Decode spikes that are not included in the encoding model
    
    Args:
        encoding_model (OfflinePPEncoder): Observered position distribution for each spike.
        spikes_to_decode (SpikeFeatures): Observered position distribution for each spike.
        decode_settings (DecodeSettings): decoder settings.
    """

    

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