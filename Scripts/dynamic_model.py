# imports
import geopandas as gpd
import pandas as pd
import numpy as np
from .network_topology import TopologyTools
from sklearn import linear_model


class SerfeModel:
    """
    This class runs the dynamic sediment balance model
    """

    def __init__(self, hydrograph, width_table, flow_exp, network, mannings_min=0.03, mannings_max=0.06, bulk_dens=1., chan_store=None):
        """

        :param hydrograph: csv file containing flow information for each gage.
        :param width_table: the width table (csv) - column 1 header: 'DA', column 2 header: 'Q', column 3 header: 'w'.
        :param flow_exp: the flow exponent from the equation in the chart that the baseline hydrology tool produced.
        :param network: drainage network shapefile
        :param mannings_min: minimum Manning's n value for the basin.
        :param mannings_max: maximum Manning's n value for the basin.
        :param bulk_dens: sediment (floodplain) deposit bulk density for the basin.
        """

        print('initiating model')
        self.hydrographs = pd.read_csv(hydrograph, index_col='Gage')
        self.flow_exp = flow_exp  # need the b in the equation for flow~DA so that you can recalculate a at each time step
        self.network = gpd.read_file(network)
        self.mannings_n = [mannings_min, mannings_max]
        self.bulk_dens = bulk_dens
        self.streams = network
        self.fp_n = 0.09  # make param
        if chan_store is not None:
            self.chan_store = np.load(chan_store)
        else:
            self.chan_store = None

        self.nt = TopologyTools(network)

        # append column to hydrographs indicating what other gages are downstream of it
        gage_ds = []
        for gage in self.hydrographs.index:
            if self.hydrographs.loc[gage, 'segid'] == -9999:
                gage_ds.append(0)
            else:
                ds_segs = self.nt.find_all_ds(self.hydrographs.loc[gage, 'segid'])
                if len(set(ds_segs) & set(self.hydrographs['segid'])) > 0:
                    gage_ds.append(len(set(ds_segs) & set(self.hydrographs['segid'])))
                else:
                    gage_ds.append(0)

        self.hydrographs['gage_ds'] = gage_ds

        print('storing topology info')
        # list of lists of upstream segments of each segment
        self.us_segs = [self.nt.find_all_us(i) for i in self.network.index]
        self.ds_segs = [self.nt.find_all_ds(i) for i in self.network.index]

        # call model for predicting channel width
        self.width = self.get_width_model(width_table)

        # set up manning's n calculation (linear function of grain size)
        self.mannings_slope = (self.mannings_n[1] - self.mannings_n[0]) / (np.max(self.network['D_pred_mid']) - np.min(self.network['D_pred_mid']))
        self.mannings_intercept = self.mannings_slope*-np.max(self.network['D_pred_mid']) + self.mannings_n[1]

        # obtain number of time steps for output table
        time = np.arange(1, self.hydrographs.shape[1]-3, 1, dtype=np.int)

        # build multi-index dataframe for storing data/outputs
        self.network = gpd.read_file(network)
        segments = np.arange(0, len(self.network.index + 1), 1)

        ydim = len(time)*len(segments)
        zeros = np.zeros((ydim, 19))

        iterables = [time, segments]
        index = pd.MultiIndex.from_product(iterables, names=['time', 'segment'])

        self.outdf = pd.DataFrame(zeros, index=index, columns=['Q', 'Qs_min', 'Qs_mid', 'Qs_max', 'Qs_out_min',
                                                               'Qs_out_mid', 'Qs_out_max', 'CSR_min', 'CSR_mid',
                                                               'CSR_max', 'Store_chan_min', 'Store_chan_mid', 'Store_chan_max',
                                                               'Store_tot_min', 'Store_tot_mid', 'Store_tot_max',
                                                               'Store_delta_min', 'Store_delta_mid', 'Store_delta_max'])  # add the names of attributes

        # set up dictionaries for tracking disturbance sediment pulses
        self.seg_dict = dict()  # mass of each sediment pulse
        for i in self.network.index:
            self.seg_dict[i] = [[0,0]]
        self.out_dict = dict()  # distance remaining that it can travel in the time step
        for i in self.network.index:
            self.out_dict[i] = []

    def get_width_model(self, width_table):
        """
        Uses regression to obtain a model for predicting width based on drainage area and discharge
        :param width_table: csv - column 1 header: 'DA', column 2 header: 'Q', column 3 header: 'w'
        :return: regression model object
        """
        table = pd.read_csv(width_table, sep=',', header=0)
        table = table.dropna(axis='columns')
        table['DA'] = np.log(table['DA'])
        table['Q'] = np.sqrt(table['Q'])

        # width regression
        regr = linear_model.LinearRegression()
        regr.fit(table[['DA', 'Q']], table['w'])
        rsq = regr.score(table[['DA', 'Q']], table['w'])
        if rsq < 0.5:
            print('R-squared is less than 0.5, poor model fit')

        print('channel width regression')
        print('intercept: ' + str(regr.intercept_))
        print('coefficient: ' + str(regr.coef_))
        print('r squared: ' + str(regr.score(table[['DA', 'Q']], table['w'])))

        return regr

    def find_flow_coef(self, Q, DA):
        """
        finds the coefficient in the drainage area - discharge relationship to extrapolate flow values
        :param Q: the discharge for a given time step
        :param DA: the drainage area of the location associated with the discharge
        :return: a coeffiecient for the drainage area - discharge relationship
        """
        a = max(Q / DA**self.flow_exp, 0)

        return a

    def get_flow(self, segid, time):

        if len(self.hydrographs.index) > 1:
            Q = []
            eff_da = self.network.loc[segid, 'eff_DA']

            r = self.hydrographs[self.hydrographs['regulated'] == 1]

            for i in r.index:  # for each gage
                if r.loc[i, 'segid'] == segid:  # if the segment id is the gage segment add flow
                    Q.append(r.loc[i, str(time)])
                elif r.loc[i, 'segid'] in self.us_segs[segid]:  # if gage is upstream of segment
                    if len(list(set(self.ds_segs[segid]) & set(r['segid']))) == r.loc[i, 'gage_ds']:  # if the amount of gages ds from seg is same as ds from given gage
                        Q.append(r.loc[i, str(time)])  # than add the stats

            ur = self.hydrographs[self.hydrographs['regulated'] == 0]
            for i in ur.index:
                if ur.loc[i, 'segid'] == segid:
                    Q.append(ur.loc[i, str(time)])
                elif ur.loc[i, 'segid'] in self.us_segs[segid]:
                    Q.append(ur.loc[i, str(time)])
                    eff_da = eff_da - ur.loc[i, 'DA']

            coefs = [self.find_flow_coef(ur.loc[x, str(time)], ur.loc[x, 'DA']) for x in ur.index]

            if len(Q) == 0:
                Q.append(0)

            #eff_da = max(self.network.loc[segid, 'eff_DA'] - np.sum(eff_da), 0.01)
            flow = np.sum(Q) + np.average(coefs)*eff_da**self.flow_exp

        else:
            coef = self.find_flow_coef(self.hydrographs.loc[self.hydrographs.index[0], str(time)], self.hydrographs.loc[self.hydrographs.index[0], 'DA'])
            flow = coef * self.network.loc[segid, 'eff_DA']**self.flow_exp

        return flow

    def get_upstream_qs(self, time, segid):
        """
        obtains the sediment flux from the adjacent upstream segment(s)
        :param time: time step
        :param segid: segment ID
        :return: sediment flux (tonnes)
        """
        us_seg = self.nt.find_us_seg(segid)
        us_seg2 = self.nt.find_us_seg2(segid)

        if us_seg is not None:
            usqs1_min = self.outdf.loc[(time, us_seg), 'Qs_out_min']
            usqs1_mid = self.outdf.loc[(time, us_seg), 'Qs_out_mid']
            usqs1_max = self.outdf.loc[(time, us_seg), 'Qs_out_max']
        else:
            usqs1_min, usqs1_mid, usqs1_max = 0., 0., 0.

        if us_seg2 is not None:
            usqs2_min = self.outdf.loc[(time, us_seg2), 'Qs_out_min']
            usqs2_mid = self.outdf.loc[(time, us_seg2), 'Qs_out_mid']
            usqs2_max = self.outdf.loc[(time, us_seg2), 'Qs_out_max']
        else:
            usqs2_min, usqs2_mid, usqs2_max = 0., 0., 0.

        usqs_tot_min = usqs1_min + usqs2_min
        usqs_tot_mid = usqs1_mid + usqs2_mid
        usqs_tot_max = usqs1_max + usqs2_max

        return usqs_tot_min, usqs_tot_mid, usqs_tot_max

    def get_direct_qs(self, segid):
        """
        obtains the hillslope sediment delivery
        :param segid: segment ID
        :return: sediment flux (tonnes)
        """
        # assume sediment bulk density
        sed_density = 2.6  # tonne/m^3

        # calculate delivery from denudation rate
        hillslope_da = self.network.loc[segid, 'direct_DA'] - (self.network.loc[segid, 'fp_area']/1000000.)
        if hillslope_da < 0:
            hillslope_da = self.network.loc[segid, 'direct_DA']*0.5
        vol_sed = ((self.network.loc[segid, 'denude']/1000.)/365.)*(hillslope_da*1000000.)  # m^3; assumes daily time step
        dir_qs = vol_sed * sed_density

        return dir_qs

    def transport_capacity(self, Q, w, S, D, om_crit_star):
        """
        calculates bedload transport capacity using Lammers and Bledsoe 2018
        :param Q: flow (cms)
        :param w: channel width
        :param S: bed slope
        :param D: median grain size
        :return: bedload transport capacity (tonnes)
        """
        # variables
        rho = 1000.
        rho_s = 2650.
        g = 9.8
        om_crit = om_crit_star * g * (rho_s - rho) * np.sqrt(((rho_s - rho)/rho) * g * D**3)

        # determine if stream power exceeds critical threshold
        om = (rho * g * Q * S) / w

        if om > om_crit:
            rate_tot = 0.0214 * (om - om_crit) ** (3. / 2.) * D ** (-1) * (Q / w) ** (-5. / 6.)  # Lammers et al total load equation (tonnes)
            cap_tot = (Q * 86400) * (rate_tot / 1000000.) * 2.6  # convert ppm to tonnes/day
            rate_bl = 0.000143 * (om - om_crit) ** (3. / 2.) * D ** (-0.5) * (Q / w) ** (-0.5)
            cap_bl = rate_bl * w * 86400. / 1000.  # convert to tonnes/day
        else:
            cap_tot = 0.
            cap_bl = 0

        return cap_tot  # add in bl stuff later

    def update_seg_dict(self, segid, vel):
        def takeSecond(elem):
            return elem[1]

        # add up stores left over from previous time step, attribute new distance based on flow velocity
        store_tot = []
        for i in self.seg_dict[segid]:
            store_tot.append(i[0])
        store_sum = np.sum(store_tot)
        if store_sum < 0:
            store_sum = 0
        elif store_sum is None:
            store_sum = 0
        self.seg_dict[segid] = [[store_sum, vel*86400-self.network.loc[segid, 'Length_m']]]

        us_seg = self.nt.find_us_seg(segid)
        us_seg2 = self.nt.find_us_seg2(segid)
        if us_seg is not None:
            for x in self.out_dict[us_seg]:
                self.seg_dict[segid].append(x)
        if us_seg2 is not None:
            for x in self.out_dict[us_seg2]:
                self.seg_dict[segid].append(x)

        self.seg_dict[segid].sort(key=takeSecond)

        new_store_tot = []
        for j in self.seg_dict[segid]:
            new_store_tot.append(j[0])
        if np.sum(new_store_tot) == 0:
            self.seg_dict[segid] = [[0,0]]

        return

    def pulse_propagate(self, segid, transport):  # make it so this calculates qs_out and adjusts transport cap for transport of non fine sed when fine is gone
        qs_out = 0

        if self.seg_dict[segid] != [[0,0]]:
            self.out_dict[segid] = []
            if transport > 0:
                for i in self.seg_dict[segid]:
                    if transport >= i[0]:
                        if i[1] - self.network.loc[segid, 'Length_m'] > 0:
                            self.out_dict[segid].append([i[0], i[1] - self.network.loc[segid, 'Length_m']])
                            qs_out += i[0]
                            i[0] = 0
                            transport = transport - i[0]
                        else:
                            pass

                    else:
                        self.out_dict[segid].append([transport, i[1] - self.network.loc[segid, 'Length_m']])
                        qs_out += transport
                        i[0] = i[0] - transport
                        transport = 0

            # get rid of any zero values in seg_dict
            #for x in self.seg_dict[segid]:
            #    if x[0] == 0:
            #        self.seg_dict[segid].remove(self.seg_dict[segid][x])
        else:
            transport = transport

        return qs_out, transport  # this value is used to adjust sed supply for model logic...

    def apply_to_reach(self, segid, time):
        """
        applies the SeRFE logic to a given reach
        :param segid: segment ID
        :param time: time step
        :return:
        """

        # get flow at reach at given time step
        flow = self.get_flow(segid, time)

        # get channel width of reach at given time step
        da = np.log(self.network.loc[segid, 'Drain_Area'])
        q = np.sqrt(flow)
        w_inputs = np.array([da, q])
        w = max(self.width.predict([w_inputs])[0], 0.5)  # 0.5 m min width
        if self.network.loc[segid, 'confine'] == 1:
            if w > self.network.loc[segid, 'w_bf']:
                w = self.network.loc[segid, 'w_bf']

        # mannings n calculation of depth
        n = self.network.loc[segid, 'D_pred_mid'] * self.mannings_slope + self.mannings_intercept

        depth_min = ((n * flow) / (w * self.network.loc[segid, 'Slope_min']**0.5))**0.6
        depth_mid = ((n * flow) / (w * self.network.loc[segid, 'Slope_mid']**0.5))**0.6
        depth_max = ((n * flow) / (w * self.network.loc[segid, 'Slope_max']**0.5))**0.6

        # find upstream qs input
        qs_us_min, qs_us_mid, qs_us_max = self.get_upstream_qs(time, segid)

        # find direct qs input (hillslopes)
        qs_dir = self.get_direct_qs(segid)  # tonnes

        if self.network.loc[segid, 'fp_area'] != 0.:
            if self.network.loc[segid, 'direct_DA'] <= 3:  # logic to sure trib contributions not in network go to channel
                qs_channel = qs_dir*self.network.loc[segid, 'confine']
                qs_fp = qs_dir - qs_channel  # tonnes
            else:
                qs_channel = max(0.8*qs_dir, qs_dir*self.network.loc[segid, 'confine'])
                qs_fp = qs_dir - qs_channel
            fp_store_min = ((self.network.loc[segid, 'fp_area']*self.network.loc[segid, 'fpt_min'])*self.bulk_dens) + qs_fp
            fp_store_mid = ((self.network.loc[segid, 'fp_area']*self.network.loc[segid, 'fpt_mid'])*self.bulk_dens) + qs_fp
            fp_store_max = ((self.network.loc[segid, 'fp_area']*self.network.loc[segid, 'fpt_max'])*self.bulk_dens) + qs_fp
            delta_fp_thick = qs_fp / self.network.loc[segid, 'fp_area'] * (1 / self.bulk_dens)
            fp_thick_min = self.network.loc[segid, 'fpt_min'] + delta_fp_thick  # meters
            fp_thick_mid = self.network.loc[segid, 'fpt_mid'] + delta_fp_thick
            fp_thick_max = self.network.loc[segid, 'fpt_max'] + delta_fp_thick

        else:
            qs_channel = qs_dir
            fp_store_min, fp_store_mid, fp_store_max = 0., 0., 0.
            fp_thick_min, fp_thick_mid, fp_thick_max = 0., 0., 0.

        # if its during disturbance period keep track of sediment pulse mass
        if self.network.loc[segid, 'dist_start'] != -9999:
            if self.network.loc[segid, 'dist_start'] <= time:
                self.seg_dict[segid].append([qs_channel, 0])
                vel = (depth_mid ** (2 / 3) * self.network.loc[segid, 'Slope_mid'] ** 0.5) / n
                self.update_seg_dict(segid, vel)

        if time == 1:
            if self.chan_store is not None:
                prev_ch_store_min = self.chan_store[segid]
                prev_ch_store_mid = self.chan_store[segid]
                prev_ch_store_max = self.chan_store[segid]
            else:
                prev_ch_store_min = 0
                prev_ch_store_mid = 0
                prev_ch_store_max = 0
        else:
            prev_ch_store_min = self.outdf.loc[(time-1, segid), 'Store_chan_min']
            prev_ch_store_mid = self.outdf.loc[(time-1, segid), 'Store_chan_mid']
            prev_ch_store_max = self.outdf.loc[(time-1, segid), 'Store_chan_max']

        # find transport capacity (including uncertainty in critical dimensionless stream power)
        S_min = self.network.loc[segid, 'Slope_min']
        S_mid = self.network.loc[segid, 'Slope_mid']
        S_max = self.network.loc[segid, 'Slope_max']
        # if there's fine sediment inputs in the channel adjust D based on proportional volume
        if self.network.loc[segid, 'dist_start'] != -9999:
            if self.network.loc[segid, 'dist_start'] <= time:
                fine_tot = []
                for i in self.seg_dict[segid]:
                    fine_tot.append(i[0])
                if np.sum(fine_tot) > 0:
                    fine_ratio_mid = np.sum(fine_tot)/(np.sum(fine_tot) + max(prev_ch_store_mid-np.sum(fine_tot), self.network.loc[segid, 'w_bf']*self.network.loc[segid, 'Length_m']*0.25*self.bulk_dens))  # minimum 25cm active layer...
                    coarse_ratio_mid = max(prev_ch_store_mid-np.sum(fine_tot), self.network.loc[segid, 'w_bf']*self.network.loc[segid, 'Length_m']*0.25*self.bulk_dens)/(max(prev_ch_store_mid-np.sum(fine_tot), self.network.loc[segid, 'w_bf']*self.network.loc[segid, 'Length_m']*0.25*self.bulk_dens)+np.sum(fine_tot))
                    fine_ratio_min = np.sum(fine_tot)/(np.sum(fine_tot) + max(prev_ch_store_min-np.sum(fine_tot), self.network.loc[segid, 'w_bf']*self.network.loc[segid, 'Length_m']*0.25*self.bulk_dens))
                    coarse_ratio_min = max(prev_ch_store_min-np.sum(fine_tot), self.network.loc[segid, 'w_bf']*self.network.loc[segid, 'Length_m']*0.25*self.bulk_dens)/(max(prev_ch_store_min-np.sum(fine_tot), self.network.loc[segid, 'w_bf']*self.network.loc[segid, 'Length_m']*0.25*self.bulk_dens)+np.sum(fine_tot))
                    fine_ratio_max = np.sum(fine_tot)/(np.sum(fine_tot) + max(prev_ch_store_max-np.sum(fine_tot), self.network.loc[segid, 'w_bf']*self.network.loc[segid, 'Length_m']*0.25*self.bulk_dens))
                    coarse_ratio_max = max(prev_ch_store_max-np.sum(fine_tot), self.network.loc[segid, 'w_bf']*self.network.loc[segid, 'Length_m']*0.25*self.bulk_dens)/(max(prev_ch_store_max-np.sum(fine_tot), self.network.loc[segid, 'w_bf']*self.network.loc[segid, 'Length_m']*0.25*self.bulk_dens)+np.sum(fine_tot))

                    D_mid = (self.network.loc[segid, 'D_pred_mid']*coarse_ratio_mid + self.network.loc[segid, 'dist_d50']*fine_ratio_mid) / 1000.
                    D_low = (self.network.loc[segid, 'D_pred_low']*coarse_ratio_max + self.network.loc[segid, 'dist_d50']*fine_ratio_max) / 1000.
                    D_high = (self.network.loc[segid, 'D_pred_hig']*coarse_ratio_min + self.network.loc[segid, 'dist_d50']*fine_ratio_min) / 1000.
                else:
                    D_mid = self.network.loc[segid, 'D_pred_mid'] / 1000.
                    D_low = self.network.loc[segid, 'D_pred_low'] / 1000.
                    D_high = self.network.loc[segid, 'D_pred_hig'] / 1000.
            else:
                D_mid = self.network.loc[segid, 'D_pred_mid'] / 1000.
                D_low = self.network.loc[segid, 'D_pred_low'] / 1000.
                D_high = self.network.loc[segid, 'D_pred_hig'] / 1000.
        else:
            D_mid = self.network.loc[segid, 'D_pred_mid'] / 1000.
            D_low = self.network.loc[segid, 'D_pred_low'] / 1000.
            D_high = self.network.loc[segid, 'D_pred_hig'] / 1000.
        cap_min = self.transport_capacity(flow, min(w, self.network.loc[segid, 'w_bf']), S_min, D_high, 0.11)
        cap_mid = self.transport_capacity(flow, min(w, self.network.loc[segid, 'w_bf']), S_mid, D_mid, 0.1)
        cap_max = self.transport_capacity(flow, min(w, self.network.loc[segid, 'w_bf']), S_max, D_low, 0.09)

        # apply transport/routing logic

        # mig_rate (could change this to just find critical unit SP using dimensionless critical SP = 0.1...)
        w_crit_min = self.width.predict([np.array([np.log(self.network.loc[segid, 'Drain_Area']), self.network.loc[segid, 'Qc_low']**0.5])])[0]
        w_crit_mid = self.width.predict([np.array([np.log(self.network.loc[segid, 'Drain_Area']), self.network.loc[segid, 'Qc_mid']**0.5])])[0]
        w_crit_max = self.width.predict([np.array([np.log(self.network.loc[segid, 'Drain_Area']), self.network.loc[segid, 'Qc_high']**0.5])])[0]
        sp_crit_min = (9810 * self.network.loc[segid, 'Qc_low'] * S_min) / w_crit_min
        sp_crit_mid = (9810 * self.network.loc[segid, 'Qc_mid'] * S_mid) / w_crit_mid
        sp_crit_max = (9810 * self.network.loc[segid, 'Qc_high'] * S_max) / w_crit_max

        excess_sp_min = float(((9810*flow*S_min)/w) - sp_crit_min*4.2)
        if excess_sp_min <= 0:
            mig_rate_min = 0
        else:
            wc = sp_crit_min * 4.2  # 1.2 is soil critical sp param, MAKE THIS PARAM
            k = 4.49E-6 + 1.74E-7*wc - 4.56E-6*self.network.loc[segid, 'Sinuos']
            mig_rate_min = k*excess_sp_min **0.5
        excess_sp_mid = float(((9810*flow*S_mid)/w) - sp_crit_mid*4.2)
        if excess_sp_mid <= 0:
            mig_rate_mid = 0
        else:
            wc = sp_crit_mid * 4.2
            k = 4.49E-6 + 1.74E-7*wc - 4.56E-6*self.network.loc[segid, 'Sinuos']
            mig_rate_mid = k*excess_sp_mid **0.5
        excess_sp_max = float(((9810*flow*S_max)/w) - sp_crit_max*4.2)
        if excess_sp_max <= 0:
            mig_rate_max = 0
        else:
            wc = sp_crit_max * 4.2
            k = 4.49E-6 + 1.74E-7*wc - 4.56E-6*self.network.loc[segid, 'Sinuos']
            mig_rate_max = k*excess_sp_max **0.5

        if self.network.loc[segid, 'dist_start'] != -9999:
            if self.network.loc[segid, 'dist_start'] <= time:
                qsout_min, transport_rem_min = self.pulse_propagate(segid, cap_min)
                qsout_max, transport_rem_max = self.pulse_propagate(segid, cap_max)
                qsout_mid, transport_rem_mid = self.pulse_propagate(segid, cap_mid)
            else:
                transport_rem_min = cap_min
                transport_rem_mid = cap_mid
                transport_rem_max = cap_max
                qsout_min, qsout_mid, qsout_max = 0, 0, 0
        else:
            transport_rem_min = cap_min
            transport_rem_mid = cap_mid
            transport_rem_max = cap_max
            qsout_min, qsout_mid, qsout_max = 0, 0, 0


        # LOW CAPACITY CASE
        # if REMAINING capacity is < all inputs MINUS fine sediment out
        if transport_rem_min < (qs_channel + qs_us_min + prev_ch_store_min)-qsout_min:  # greater sediment load than transport capacity
            qs_out = transport_rem_min + qsout_min
            if self.network.loc[segid, 'confine'] != 1.:  # segment is unconfined
                if depth_min < fp_thick_min:  # depth is less than floodplain height
                    channel_store = (qs_channel + qs_us_min + prev_ch_store_min) - qs_out
                    delta_h = ((channel_store - prev_ch_store_min) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_min'] = self.network.loc[segid, 'Slope_min'] + (delta_h/self.network.loc[segid, 'Length_m'])
                else:  # depth is greater than floodplain height
                    vol_channel = depth_min * self.network.loc[segid, 'Length_m'] * min(w, self.network.loc[segid, 'w_bf'])
                    vol_fp = (depth_min - fp_thick_min)*self.network.loc[segid, 'fp_area']
                    v_chan = (depth_min ** (2 / 3) * S_min ** 0.5) / n
                    v_fp = ((depth_min - fp_thick_min) ** (2 / 3) * S_min ** 0.5) / self.fp_n
                    v_ratio = v_fp / v_chan
                    fp_ratio = vol_fp*v_ratio / ((vol_channel + vol_fp)-(vol_fp*v_ratio)) # correct volumes for velocity
                    sed_remain = (qs_channel + qs_us_min + prev_ch_store_min) - qs_out
                    channel_store = sed_remain * (1-fp_ratio)
                    delta_h = ((channel_store - prev_ch_store_min) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_min'] = self.network.loc[segid, 'Slope_min'] + (delta_h/self.network.loc[segid, 'Length_m'])
                    fp_store_min = fp_store_min + (sed_remain - channel_store)
                    fp_thick_min = fp_store_min / self.network.loc[segid, 'fp_area'] * (1/self.bulk_dens)
            else:  # segment is confined
                channel_store = (qs_channel + qs_us_min + prev_ch_store_min) - qs_out
                delta_h = ((channel_store - prev_ch_store_min) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                self.network.loc[segid, 'Slope_min'] = self.network.loc[segid, 'Slope_min'] + (delta_h/self.network.loc[segid, 'Length_m'])

            if (qs_channel + qs_us_min + prev_ch_store_min) == 0:
                if cap_min > 0:
                    csr = cap_min
                else:
                    csr = 1
            else:
                csr = cap_min / (qs_channel + qs_us_min + prev_ch_store_min)

        elif transport_rem_min > (qs_channel + qs_us_min + prev_ch_store_min)-qsout_min:  # greater transport capacity than sediment load
            if self.network.loc[segid, 'confine'] != 1.:  # segment is unconfined
                fp_recr = min((mig_rate_min * 86400) * (self.network.loc[segid, 'Length_m'] * (1 - self.network.loc[segid, 'confine'])) * fp_thick_min * self.bulk_dens, self.network.loc[segid, 'fp_area'] * self.network.loc[segid, 'fpt_min'] * self.bulk_dens)  # set up as 1 DAY TIME STEP
                if depth_min < fp_thick_min:
                    qs_out = (qs_channel + qs_us_min + prev_ch_store_min) + fp_recr + qsout_min
                    fp_store_min = fp_store_min - fp_recr
                    channel_store = 0.
                    delta_h = ((channel_store - prev_ch_store_min) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_min'] = self.network.loc[segid, 'Slope_min'] + (delta_h/self.network.loc[segid, 'Length_m'])
                    # update fp area...?  if i hold width constant that the change is storage should be represented by lower fp surface (see how line 166 elevates it.
                    fp_recr_thick = fp_recr / self.network.loc[segid, 'fp_area'] * (1/self.bulk_dens)
                    fp_thick_min = max(0, fp_thick_min - fp_recr_thick)
                else:
                    vol_channel = depth_min * self.network.loc[segid, 'Length_m'] * min(w, self.network.loc[segid, 'w_bf'])
                    vol_fp = (depth_min - fp_thick_min) * self.network.loc[segid, 'fp_area']
                    v_chan = (depth_min ** (2 / 3) * S_min ** 0.5) / n
                    v_fp = ((depth_min - fp_thick_min) ** (2 / 3) * S_min ** 0.5) / self.fp_n
                    v_ratio = v_fp / v_chan
                    fp_ratio = vol_fp * v_ratio / ((vol_channel + vol_fp) - (vol_fp * v_ratio))  # correct volumes for velocity
                    w_s = (16.17*0.0008**2)/(1.8e-5+(12.1275*0.0008**3)**0.5)  # suspended grain size 0.8mm for this trajectory
                    fp_v_ratio = min(w_s/v_fp, 0.01)  # 1 percent minimum
                    fp_recr = fp_recr - (qs_us_min*fp_ratio*fp_v_ratio)
                    qs_out = (qs_channel + qs_us_min + prev_ch_store_min) + fp_recr + qsout_min
                    fp_store_min = fp_store_min - fp_recr
                    channel_store = 0.
                    delta_h = ((channel_store - prev_ch_store_min) * (1 / self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_min'] = self.network.loc[segid, 'Slope_min'] + (delta_h / self.network.loc[segid, 'Length_m'])
                    # update fp area...?  if i hold width constant that the change is storage should be represented by lower fp surface (see how line 166 elevates it.
                    fp_recr_thick = fp_recr / self.network.loc[segid, 'fp_area'] * (1 / self.bulk_dens)
                    fp_thick_min = max(0, fp_thick_min - fp_recr_thick)

                if (qs_channel + qs_us_min + prev_ch_store_min + fp_recr) == 0:
                    if cap_min > 0:
                        csr = cap_min
                    else:
                        csr = 1
                else:
                    csr = cap_min / (qs_channel + qs_us_min + prev_ch_store_min + fp_recr)

            else:  # segment is confined
                channel_store = 0.
                delta_h = ((channel_store - prev_ch_store_min) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                self.network.loc[segid, 'Slope_min'] = self.network.loc[segid, 'Slope_min'] + (delta_h/self.network.loc[segid, 'Length_m'])
                qs_out = (qs_channel + qs_us_min + prev_ch_store_min) + qsout_min

                if (qs_channel + qs_us_min + prev_ch_store_min) == 0:
                    if cap_min > 0:
                        csr = cap_min
                    else:
                        csr = 1
                else:
                    csr = cap_min / (qs_channel + qs_us_min + prev_ch_store_min)

        else:  # sediment load equals transport capacity
            qs_out = qs_channel + qs_us_min + prev_ch_store_min
            channel_store = 0.
            csr = 1.

        # if you are at a dam, qs_out = 0
        next_reach = self.nt.get_next_reach(segid)
        if next_reach is not None:
            if self.network.loc[next_reach, 'eff_DA'] < self.network.loc[segid, 'eff_DA']:
                qs_out = 0.

        store_tot = channel_store + fp_store_min

        if self.network.loc[segid, 'confine'] != 1.:
            self.network.loc[segid, 'fpt_min'] = fp_thick_min

        # update output table
        self.outdf.loc[(time, segid), 'Q'] = flow
        self.outdf.loc[(time, segid), 'Qs_min'] = qs_channel + qs_us_min + prev_ch_store_min
        self.outdf.loc[(time, segid), 'Qs_out_min'] = qs_out
        self.outdf.loc[(time, segid), 'CSR_min'] = csr
        self.outdf.loc[(time, segid), 'Store_tot_min'] = store_tot
        self.outdf.loc[(time, segid), 'Store_chan_min'] = channel_store
        if time > 1:
            self.outdf.loc[(time, segid), 'Store_delta_min'] = store_tot - (self.outdf.loc[(time-1, segid), 'Store_tot_min'])
        else:
            self.outdf.loc[(time, segid), 'Store_delta_min'] = 0  # this is wrong channel storage can change day 1

        # MID CAPACITY CASE
        if transport_rem_mid < (qs_channel + qs_us_mid + prev_ch_store_mid) - qsout_mid:  # greater sediment load than transport capacity
            qs_out = cap_mid + qsout_mid
            if self.network.loc[segid, 'confine'] != 1.:  # segment is unconfined
                if depth_mid < fp_thick_mid:  # depth is less than floodplain height
                    channel_store = (qs_channel + qs_us_mid + prev_ch_store_mid) - qs_out
                    delta_h = ((channel_store - prev_ch_store_mid) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_mid'] = self.network.loc[segid, 'Slope_mid'] + (delta_h / self.network.loc[segid, 'Length_m'])
                else:  # depth is greater than floodplain height
                    vol_channel = depth_mid * self.network.loc[segid, 'Length_m'] * min(w, self.network.loc[segid, 'w_bf'])
                    vol_fp = (depth_mid - fp_thick_mid) * self.network.loc[segid, 'fp_area']
                    v_chan = (depth_mid ** (2 / 3) * S_mid ** 0.5) / n
                    v_fp = ((depth_mid - fp_thick_mid) ** (2 / 3) * S_mid ** 0.5) / self.fp_n
                    v_ratio = v_fp / v_chan
                    fp_ratio = vol_fp * v_ratio / ((vol_channel + vol_fp) - (vol_fp * v_ratio))  # correct volumes for velocity
                    sed_remain = (qs_channel + qs_us_mid + prev_ch_store_mid) - qs_out
                    channel_store = sed_remain * (1 - fp_ratio)
                    delta_h = ((channel_store - prev_ch_store_mid) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_mid'] = self.network.loc[segid, 'Slope_mid'] + (delta_h / self.network.loc[segid, 'Length_m'])
                    fp_store_mid = fp_store_mid + (sed_remain - channel_store)
                    fp_thick_mid = fp_store_mid / self.network.loc[segid, 'fp_area'] * (1/self.bulk_dens)
            else:  # segment is confined
                channel_store = (qs_channel + qs_us_mid + prev_ch_store_mid) - qs_out
                delta_h = ((channel_store - prev_ch_store_mid) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                self.network.loc[segid, 'Slope_mid'] = self.network.loc[segid, 'Slope_mid'] + (delta_h / self.network.loc[segid, 'Length_m'])

            if (qs_channel + qs_us_mid + prev_ch_store_mid) == 0:
                if cap_mid > 0:
                    csr = cap_mid
                else:
                    csr = 1
            else:
                csr = cap_mid / (qs_channel + qs_us_mid + prev_ch_store_mid)

        elif transport_rem_mid > (qs_channel + qs_us_mid + prev_ch_store_mid) - qsout_mid:  # greater transport capacity than sediment load
            if self.network.loc[segid, 'confine'] != 1.:  # segment is unconfined
                fp_recr = min((mig_rate_mid * 86400) * (self.network.loc[segid, 'Length_m'] * (
                            1 - self.network.loc[segid, 'confine'])) * fp_thick_mid * self.bulk_dens,
                              self.network.loc[segid, 'fp_area'] * self.network.loc[
                                  segid, 'fpt_mid'] * self.bulk_dens)  # set up as 1 DAY TIME STEP
                if depth_mid < fp_thick_mid:
                    qs_out = (qs_channel + qs_us_mid + prev_ch_store_mid) + fp_recr + qsout_mid
                    fp_store_mid = fp_store_mid - fp_recr
                    channel_store = 0.
                    delta_h = ((channel_store - prev_ch_store_mid) * (1 / self.bulk_dens)) / (
                                0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_mid'] = self.network.loc[segid, 'Slope_mid'] + (
                                delta_h / self.network.loc[segid, 'Length_m'])
                    # update fp area...?  if i hold width constant that the change is storage should be represented by lower fp surface (see how line 166 elevates it.
                    fp_recr_thick = fp_recr / self.network.loc[segid, 'fp_area'] * (1 / self.bulk_dens)
                    fp_thick_mid = max(0, fp_thick_mid - fp_recr_thick)
                else:
                    vol_channel = depth_mid * self.network.loc[segid, 'Length_m'] * min(w, self.network.loc[segid, 'w_bf'])
                    vol_fp = (depth_mid - fp_thick_mid) * self.network.loc[segid, 'fp_area']
                    v_chan = (depth_mid ** (2 / 3) * S_mid ** 0.5) / n
                    v_fp = ((depth_mid - fp_thick_mid) ** (2 / 3) * S_mid ** 0.5) / self.fp_n
                    v_ratio = v_fp / v_chan
                    fp_ratio = vol_fp * v_ratio / ((vol_channel + vol_fp) - (vol_fp * v_ratio))  # correct volumes for velocity
                    w_s = (16.17 * 0.0005 ** 2) / (1.8e-5 + (12.1275 * 0.0005 ** 3) ** 0.5)  # suspended grain size 0.5mm for this trajectory
                    fp_v_ratio = min(w_s / v_fp, 0.01)  # 1 percent minimum
                    fp_recr = fp_recr - (qs_us_min * fp_ratio * fp_v_ratio)
                    qs_out = (qs_channel + qs_us_mid + prev_ch_store_mid) + fp_recr + qsout_mid
                    fp_store_mid = fp_store_mid - fp_recr
                    channel_store = 0.
                    delta_h = ((channel_store - prev_ch_store_mid) * (1 / self.bulk_dens)) / (
                                0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_mid'] = self.network.loc[segid, 'Slope_mid'] + (
                                delta_h / self.network.loc[segid, 'Length_m'])
                    # update fp area...?  if i hold width constant that the change is storage should be represented by lower fp surface (see how line 166 elevates it.
                    fp_recr_thick = fp_recr / self.network.loc[segid, 'fp_area'] * (1 / self.bulk_dens)
                    fp_thick_mid = max(0, fp_thick_mid - fp_recr_thick)

                if (qs_channel + qs_us_mid + prev_ch_store_mid + fp_recr) == 0:
                    if cap_mid > 0:
                        csr = cap_mid
                    else:
                        csr = 1
                else:
                    csr = cap_mid / (qs_channel + qs_us_mid + prev_ch_store_mid + fp_recr)

            else:  # segment is confined
                channel_store = 0.
                delta_h = ((channel_store - prev_ch_store_mid) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                self.network.loc[segid, 'Slope_mid'] = self.network.loc[segid, 'Slope_mid'] + (delta_h / self.network.loc[segid, 'Length_m'])
                qs_out = (qs_channel + qs_us_mid + prev_ch_store_mid) + qsout_mid

                if (qs_channel + qs_us_mid + prev_ch_store_mid) == 0:
                    if cap_mid > 0:
                        csr = cap_mid
                    else:
                        csr = 1
                else:
                    csr = cap_mid / (qs_channel + qs_us_mid + prev_ch_store_mid)

        else:  # sediment load equals transport capacity
            qs_out = qs_channel + qs_us_mid + prev_ch_store_mid
            channel_store = 0.
            csr = 1.

        # if you are at a dam, qs_out = 0
        next_reach = self.nt.get_next_reach(segid)
        if next_reach is not None:
            if self.network.loc[next_reach, 'eff_DA'] < self.network.loc[segid, 'eff_DA']:
                qs_out = 0.

        store_tot = channel_store + fp_store_mid

        if self.network.loc[segid, 'confine'] != 1.:
            self.network.loc[segid, 'fpt_mid'] = fp_thick_mid

            # update output table
        self.outdf.loc[(time, segid), 'Qs_mid'] = qs_channel + qs_us_mid + prev_ch_store_mid
        self.outdf.loc[(time, segid), 'Qs_out_mid'] = qs_out
        self.outdf.loc[(time, segid), 'CSR_mid'] = csr
        self.outdf.loc[(time, segid), 'Store_tot_mid'] = store_tot
        self.outdf.loc[(time, segid), 'Store_chan_mid'] = channel_store
        if time > 1:
            self.outdf.loc[(time, segid), 'Store_delta_mid'] = store_tot - (self.outdf.loc[(time - 1, segid), 'Store_tot_mid'])
        else:
            self.outdf.loc[(time, segid), 'Store_delta_mid'] = 0

        # HIGH CAPACITY CASE
        if transport_rem_max < (qs_channel + qs_us_max + prev_ch_store_max) - qsout_max:  # greater sediment load than transport capacity
            qs_out = cap_max + qsout_max
            if self.network.loc[segid, 'confine'] != 1.:  # segment is unconfined
                if depth_max < fp_thick_max:  # depth is less than floodplain height
                    channel_store = (qs_channel + qs_us_max + prev_ch_store_max) - qs_out
                    delta_h = ((channel_store - prev_ch_store_max) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_max'] = self.network.loc[segid, 'Slope_max'] + (delta_h / self.network.loc[segid, 'Length_m'])
                else:  # depth is greater than floodplain height
                    vol_channel = depth_max * self.network.loc[segid, 'Length_m'] * min(w, self.network.loc[segid, 'w_bf'])
                    vol_fp = (depth_max - fp_thick_max) * self.network.loc[segid, 'fp_area']
                    v_chan = (depth_max ** (2 / 3) * S_max ** 0.5) / n
                    v_fp = ((depth_max - fp_thick_max) ** (2 / 3) * S_max ** 0.5) / self.fp_n
                    v_ratio = v_fp / v_chan
                    fp_ratio = vol_fp * v_ratio / ((vol_channel + vol_fp) - (vol_fp * v_ratio))  # correct volumes for velocity
                    sed_remain = (qs_channel + qs_us_max + prev_ch_store_max) - qs_out
                    channel_store = sed_remain * (1 - fp_ratio)
                    delta_h = ((channel_store - prev_ch_store_max) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_max'] = self.network.loc[segid, 'Slope_max'] + (delta_h / self.network.loc[segid, 'Length_m'])
                    fp_store_max = fp_store_max + (sed_remain - channel_store)
                    fp_thick_max = fp_store_max / self.network.loc[segid, 'fp_area'] * (1/self.bulk_dens)
            else:  # segment is confined
                channel_store = (qs_channel + qs_us_max + prev_ch_store_max) - qs_out
                delta_h = ((channel_store - prev_ch_store_max) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                self.network.loc[segid, 'Slope_max'] = self.network.loc[segid, 'Slope_max'] + (delta_h / self.network.loc[segid, 'Length_m'])

            if (qs_channel + qs_us_max + prev_ch_store_max) == 0:
                if cap_max > 0:
                    csr = cap_max
                else:
                    csr = 1
            else:
                csr = cap_max / (qs_channel + qs_us_max + prev_ch_store_max)

        elif transport_rem_max > (qs_channel + qs_us_max + prev_ch_store_max) - qsout_max:  # greater transport capacity than sediment load
            if self.network.loc[segid, 'confine'] != 1.:  # segment is unconfined
                fp_recr = min((mig_rate_max * 86400) * (self.network.loc[segid, 'Length_m'] * (
                            1 - self.network.loc[segid, 'confine'])) * fp_thick_max * self.bulk_dens,
                              self.network.loc[segid, 'fp_area'] * self.network.loc[
                                  segid, 'fpt_max'] * self.bulk_dens)  # set up as 1 DAY TIME STEP
                if depth_max < fp_thick_max:
                    qs_out = (qs_channel + qs_us_max + prev_ch_store_max) + fp_recr + qsout_max
                    fp_store_max = fp_store_max - fp_recr
                    channel_store = 0.
                    delta_h = ((channel_store - prev_ch_store_max) * (1 / self.bulk_dens)) / (
                                0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_max'] = self.network.loc[segid, 'Slope_max'] + (
                                delta_h / self.network.loc[segid, 'Length_m'])
                    # update fp area...?  if i hold width constant that the change is storage should be represented by lower fp surface (see how line 166 elevates it.
                    fp_recr_thick = fp_recr / self.network.loc[segid, 'fp_area'] * (1 / self.bulk_dens)
                    fp_thick_max = max(0, fp_thick_max - fp_recr_thick)
                else:
                    vol_channel = depth_max * self.network.loc[segid, 'Length_m'] * min(w, self.network.loc[segid, 'w_bf'])
                    vol_fp = (depth_max - fp_thick_max) * self.network.loc[segid, 'fp_area']
                    v_chan = (depth_max ** (2 / 3) * S_max ** 0.5) / n
                    v_fp = ((depth_max - fp_thick_max) ** (2 / 3) * S_max ** 0.5) / self.fp_n
                    v_ratio = v_fp / v_chan
                    fp_ratio = vol_fp * v_ratio / ((vol_channel + vol_fp) - (vol_fp * v_ratio))  # correct volumes for velocity
                    w_s = (16.17 * 0.0003 ** 2) / (1.8e-5 + (12.1275 * 0.0003 ** 3) ** 0.5)  # suspended grain size 0.3mm for this trajectory
                    fp_v_ratio = min(w_s / v_fp, 0.01)  # 1 percent minimum
                    fp_recr = fp_recr - (qs_us_min * fp_ratio * fp_v_ratio)
                    qs_out = (qs_channel + qs_us_max + prev_ch_store_max) + fp_recr + qsout_max
                    fp_store_max = fp_store_max - fp_recr
                    channel_store = 0.
                    delta_h = ((channel_store - prev_ch_store_max) * (1 / self.bulk_dens)) / (
                                0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                    self.network.loc[segid, 'Slope_max'] = self.network.loc[segid, 'Slope_max'] + (
                                delta_h / self.network.loc[segid, 'Length_m'])
                    # update fp area...?  if i hold width constant that the change is storage should be represented by lower fp surface (see how line 166 elevates it.
                    fp_recr_thick = fp_recr / self.network.loc[segid, 'fp_area'] * (1 / self.bulk_dens)
                    fp_thick_max = max(0, fp_thick_max - fp_recr_thick)

                if (qs_channel + qs_us_max + prev_ch_store_max + fp_recr) == 0:
                    if cap_max > 0:
                        csr = cap_max
                    else:
                        csr = 1
                else:
                    csr = cap_max / (qs_channel + qs_us_max + prev_ch_store_max + fp_recr)

            else:  # segment is confined
                channel_store = 0.
                delta_h = ((channel_store - prev_ch_store_max) * (1/self.bulk_dens)) / (0.5 * self.network.loc[segid, 'w_bf'] * self.network.loc[segid, 'Length_m'])
                self.network.loc[segid, 'Slope_max'] = self.network.loc[segid, 'Slope_max'] + (delta_h / self.network.loc[segid, 'Length_m'])
                qs_out = (qs_channel + qs_us_max + prev_ch_store_max) + qsout_max

                if (qs_channel + qs_us_max + prev_ch_store_max) == 0:
                    if cap_max > 0:
                        csr = cap_max
                    else:
                        csr = 1
                else:
                    csr = cap_max / (qs_channel + qs_us_max + prev_ch_store_max)

        else:  # sediment load equals transport capacity
            qs_out = qs_channel + qs_us_max + prev_ch_store_max
            channel_store = 0.
            csr = 1.

        # if you are at a dam, qs_out = 0
        next_reach = self.nt.get_next_reach(segid)
        if next_reach is not None:
            if self.network.loc[next_reach, 'eff_DA'] < self.network.loc[segid, 'eff_DA']:
                qs_out = 0.

        store_tot = channel_store + fp_store_max

        if self.network.loc[segid, 'confine'] != 1.:
            self.network.loc[segid, 'fpt_max'] = fp_thick_max

            # update output table
        self.outdf.loc[(time, segid), 'Qs_max'] = qs_channel + qs_us_max + prev_ch_store_max
        self.outdf.loc[(time, segid), 'Qs_out_max'] = qs_out
        self.outdf.loc[(time, segid), 'CSR_max'] = csr
        self.outdf.loc[(time, segid), 'Store_tot_max'] = store_tot
        self.outdf.loc[(time, segid), 'Store_chan_max'] = channel_store
        if time > 1:
            self.outdf.loc[(time, segid), 'Store_delta_max'] = store_tot - (self.outdf.loc[(time - 1, segid), 'Store_tot_max'])
        else:
            self.outdf.loc[(time, segid), 'Store_delta_max'] = 0

        return

    def run_first_order(self, time):
        """
        runs the model for all first order stream segments
        :param time: time step
        :return:
        """
        seg = self.nt.seg_id_from_rid('1.1')
        time = time

        while seg is not None:
            self.apply_to_reach(seg, time)

            next_reach = self.nt.get_next_reach(seg)

            if next_reach is not None:
                if self.network.loc[next_reach, 'confluence'] == 1:
                    next_reach = self.nt.get_next_chain(next_reach)

            else:
                next_reach = self.nt.get_next_chain(seg)

            seg = next_reach

        return

    def run_below_confluences(self, time):
        """
        runs the model for all stream segments below confluences (greater than first order)
        :param time: time step
        :return:
        """
        conf_list = self.network[self.network['confluence'] == 1].index
        da_vals = [self.network.loc[i, 'Drain_Area'] for i in conf_list]

        sort = np.argsort(da_vals)
        conf_list = [conf_list[i] for i in sort]

        while len(conf_list) > 0:
            for x in conf_list:
                seg = x
                us = self.nt.find_us_seg(seg)
                us2 = self.nt.find_us_seg2(seg)

                if self.outdf.loc[(time, us), 'Qs_out_mid'] == -9999 or self.outdf.loc[(time, us2), 'Qs_out_mid'] == -9999:
                    pass
                else:
                    time = time

                    while seg is not None:
                        self.apply_to_reach(seg, time)

                        next_reach = self.nt.get_next_reach(seg)

                        if next_reach is not None:
                            if self.network.loc[next_reach, 'confluence'] == 1:
                                next_reach = None
                            else:
                                pass
                        else:
                            pass

                        seg = next_reach

                    conf_list.remove(x)

        return

    def run_model(self, spinup=False):
        """
        method that runs the dynamic SeRFE model
        :param spinup: boolean - True of running spinup period, False if saving outputs
        :return: a dataframe with two index columns (Segment ID and time step) containing model outputs for each segment
        """
        total_t = self.hydrographs.shape[1]-4
        time = 1

        while time <= total_t:
            print('day ' + str(time))

            # set qs_out initially to -9999
            for i in range(len(self.outdf.index.levels[1])):
                self.outdf.loc[(time, i), 'Qs_out_min'] = -9999
                self.outdf.loc[(time, i), 'Qs_out_mid'] = -9999
                self.outdf.loc[(time, i), 'Qs_out_max'] = -9999

            # apply denudation rate to each segment
            for i in self.network.index:
                if self.network.loc[i, 'dist_start'] != -9999:
                    if time in range(int(self.network.loc[i, 'dist_start']), int((self.network.loc[i, 'dist_end']+1))):
                        self.network.loc[i, 'denude'] = np.random.gamma(self.network.loc[i, 'dist_g_sh'], self.network.loc[i, 'dist_g_sc'])
                    else:
                        self.network.loc[i, 'denude'] = np.random.gamma(self.network.loc[i, 'g_shape'], self.network.loc[i, 'g_scale'])
                else:
                    self.network.loc[i, 'denude'] = np.random.gamma(self.network.loc[i, 'g_shape'], self.network.loc[i, 'g_scale'])

            # run the model for given time step
            print('running first order')
            self.run_first_order(time)

            print('running below confluences')
            self.run_below_confluences(time)

            time += 1
            # reset denude rates to -9999, do I need to do this or will it just overwrite?

        if spinup:
            for i in self.network.index:
                self.network.loc[i, 'Slope_min'] = self.network.loc[i, 'Slope_mid']
                self.network.loc[i, 'Slope_max'] = self.network.loc[i, 'Slope_mid']
                self.network.loc[i, 'fpt_min'] = self.network.loc[i, 'fpt_mid']
                self.network.loc[i, 'fpt_max'] = self.network.loc[i, 'fpt_mid']

            self.network.to_file(self.streams)

            chan_stor = []
            for x in self.outdf.index.levels[1]:
                val = self.outdf.loc[(total_t, x), 'Store_chan_mid']
                chan_stor.append(val)

            return chan_stor

        else:

            return self.outdf
