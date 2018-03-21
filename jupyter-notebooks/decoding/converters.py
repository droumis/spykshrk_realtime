import numpy as np
import re
import pandas as pd

def multiunit_to_spykshrk(mu_times, ntrode_keys):
    '''converting Eric DeNovellis dataframe format to spykshrk format

    Parameters
    ----------
    mu_times : ntrodes list of dataframe multiunit marks
    ntrode_keys : tuples 

    Returns
    -------
    allmu : multiindex dataframe of ntrode marks in spykshrk format

    '''
    allmu = []
    for introde, ntrode_key in enumerate(ntrode_keys):
        # format time multiindex
        ntrode = ntrode_key[-1]
        testmu = mu_times[ntrode-1].astype('int16').copy()
        testmu['time'] = np.round(testmu.index.total_seconds(), decimals=5)
        testmu.drop_duplicates(subset='time', inplace=True)
        testmu['timestamp'] = testmu.time.apply(lambda row: (row*1e5)).astype('uint64') #in ns
        testmu.set_index(['time', 'timestamp'], inplace=True) 

        ### re-label channels to c00, c01, c02 .. cn
        testmu.rename(columns=lambda x: 'c%02d' % (int(re.findall(r'\d+',x)[0])-1), inplace=True)

        # join ntrode info
        testmu['day'] = ntrode_key[1]
        testmu['epoch'] = ntrode_key[2]
        testmu['elec_grp_id'] = ntrode_key[3]
        testmu.set_index(['day', 'epoch', 'elec_grp_id'], append=True, inplace=True)
        testmu = testmu.reorder_levels(['day', 'epoch', 'elec_grp_id', 'timestamp', 'time'])
        allmu.append(testmu)
    return pd.concat(allmu)

def linear_position_to_spykshrk(position_df,ntrode_key):
    '''converting Eric DeNovellis dataframe format to spykshrk format

    Parameters
    ----------
    position_df : dataframe
    ntrode_keys : tuples 

    Returns
    -------
    tetpos : multiindex dataframe

    '''
    testpos = position_df[['linear_distance', 'speed', 'labeled_segments']]
    testpos.columns= ['linpos_flat', 'linvel_flat', 'seg_idx']
    testpos['time'] = testpos.index.total_seconds()
    testpos['timestamp'] = testpos.time.apply(lambda row: (row*1e5)).astype('uint64')
    testpos['day'] = ntrode_key[1]
    testpos['epoch'] = ntrode_key[2]
    testpos.set_index(['day', 'epoch', 'timestamp', 'time'], append=False, inplace=True)
    testpos = testpos.reorder_levels(['day', 'epoch', 'timestamp', 'time'])
    return testpos