### CHECK FACTS: https://www.epa.gov/greenvehicles/greenhouse-gas-emissions-typical-passenger-vehicle
### FUEL ECONOMY: 22 miles per gallon
### 8,887 grams CO2/ gallon
### 404 grams per mile per car
import json
import sys
import numpy as np
import scipy.sparse 
import scipy.io as sio 
import time 
import os
import logging
import datetime
import warnings
import pandas as pd 
import sf_abm
import matplotlib.pyplot as plt
import matplotlib.cm as cm 
import gc

plt.rcParams.update({'font.size': 15, 'font.weight': "normal", 'font.family':'serif', 'axes.linewidth': 0.1})

pd.set_option('display.max_columns', 10)

absolute_path = os.path.dirname(os.path.abspath(__file__))
folder = 'sf_overpass'
scenario = 'original'
outdir = 'output_Jun2019'

highway_type = ['motorway', 'motorway_link', 'trunk', 'trunk_link']

def base_co2(mph_array):
    ### CO2 - speed function constants (Barth and Boriboonsomsin, "Real-World Carbon Dioxide Impacts of Traffic Congestion")
    b0 = 7.362867270508520
    b1 = -0.149814315838651
    b2 = 0.004214810510200
    b3 = -0.000049253951464
    b4 = 0.000000217166574
    return np.exp(b0 + b1*mph_array + b2*mph_array**2 + b3*mph_array**3 + b4*mph_array**4)

def aad_vol_vmt_baseemi(aad_df, hour_volume_df):

    aad_df = pd.merge(aad_df, hour_volume_df, on='edge_id_igraph', how='left')
    aad_df['vht'] = aad_df['true_flow'] * aad_df['t_avg']/3600
    aad_df['v_avg_mph'] = aad_df['length']/aad_df['t_avg'] * 2.23694 ### time step link speed in mph
    aad_df['base_co2'] = base_co2(aad_df['v_avg_mph']) ### link-level co2 eimission in gram per mile per vehicle
    ### correction for slope
    aad_df['base_co2'] = aad_df['base_co2'] * aad_df['slope_factor']
    aad_df['base_emi'] = aad_df['base_co2'] * aad_df['length'] /1609.34 * aad_df['true_flow'] ### speed related CO2 x length x flow. Final results unit is gram.

    aad_df['aad_vol'] += aad_df['true_flow']
    aad_df['aad_vht'] += aad_df['vht']
    aad_df['aad_vmt'] += aad_df['true_flow']*aad_df['length']
    aad_df['aad_base_emi'] += aad_df['base_emi']
    aad_df = aad_df[['edge_id_igraph', 'length', 'type', 'slope_factor', 'aad_vol', 'aad_vht', 'aad_vmt', 'aad_base_emi']]
    return aad_df

def preprocessing(offset=True):
    ### Read the edge attributes. 
    edges_df = pd.read_csv(absolute_path+'/../0_network/data/{}/{}/edges_elevation.csv'.format(folder, scenario))
    edges_df = edges_df[['edge_id_igraph', 'start_sp', 'end_sp', 'length', 'lanes', 'slope', 'capacity', 'fft', 'type', 'geometry']]
    edges_df['slope_factor'] = np.where(edges_df['slope']<-0.05, 0.2, np.where(edges_df['slope']>0.15, 3.4, 1+0.16*(edges_df['slope']*100)))

    ### PCI RELATED EMISSION
    ### Read pavement age on Jan 01, 2017, and degradation model coefficients
    sf_pavement = pd.read_csv(absolute_path+'/input/r_to_python_20190323.csv')
    sf_pavement['initial_age'] *= 365
    sf_pavement['ispublicworks'] = 1

    ### Key to merge cnn with igraphid
    sf_cnn_igraphid = pd.read_csv(absolute_path+'/input/3_cnn_igraphid.csv')
    sf_cnn_igraphid = sf_cnn_igraphid[sf_cnn_igraphid['edge_id_igraph']!='None'].reset_index()
    sf_cnn_igraphid['edge_id_igraph'] = sf_cnn_igraphid['edge_id_igraph'].astype('int64')
    ### Get degradation related parameters, incuding the coefficients and initial age
    edges_df = pd.merge(edges_df, sf_cnn_igraphid, on='edge_id_igraph', how='left')
    
    ### Fill cnn na with edge_id_igraph
    edges_df['cnn_expand'] = np.where(pd.isna(edges_df['cnn']), edges_df['edge_id_igraph'], edges_df['cnn'])
    edges_df = pd.merge(edges_df, sf_pavement[['cnn', 'ispublicworks', 'stfcbr', 'alpha', 'beta', 'xi', 'uv', 'initial_age']], left_on='cnn_expand', right_on='cnn', how='left')
    edges_df['cnn_expand'] = edges_df['cnn_expand'].astype(int).astype(str)

    ### Keep relevant colum_ns
    edges_df = edges_df[['edge_id_igraph', 'start_sp', 'end_sp', 'length', 'lanes', 'slope', 'slope_factor', 'capacity', 'fft', 'cnn_expand', 'ispublicworks', 'stfcbr', 'alpha', 'beta', 'xi', 'uv', 'initial_age', 'type', 'geometry']]
    ### Remove duplicates
    edges_df = edges_df.drop_duplicates(subset='edge_id_igraph', keep='first').reset_index()

    ### Some igraphids have empty coefficients and age, set to average
    #edges_df['initial_age'] = edges_df['initial_age'].fillna(edges_df['initial_age'].mean())
    edges_df['initial_age'] = edges_df['initial_age'].fillna(0)
    edges_df['alpha'] = edges_df['alpha'].fillna(edges_df['alpha'].mean())
    edges_df['beta'] = edges_df['beta'].fillna(edges_df['beta'].mean())
    edges_df['xi'] = edges_df['xi'].fillna(0)
    edges_df['uv'] = edges_df['uv'].fillna(0)
    edges_df['intercept'] = edges_df['alpha'] + edges_df['xi']
    if offset:
        edges_df['intercept'] -= 5.5 ### to match sf public works record
    edges_df['slope'] = edges_df['beta'] + edges_df['uv']

    ### Not considering highways
    edges_df['initial_age'] = np.where(edges_df['type'].isin(highway_type), 0, edges_df['initial_age'])
    edges_df['intercept'] = np.where(edges_df['type'].isin(highway_type), 85, edges_df['intercept']) ### highway PCI is assumed to be 85 throughout based on Caltrans 2015 state of the pavement report
    edges_df['slope'] = np.where(edges_df['type'].isin(highway_type), 0, edges_df['slope'])

    ### Not considering non publicwork roads
    edges_df['initial_age'] = np.where(edges_df['ispublicworks']==0, 0, edges_df['initial_age'])
    edges_df['intercept'] = np.where(edges_df['ispublicworks']==0, 100, edges_df['intercept']) ### highway PCI is assumed to be 85 throughout based on Caltrans 2015 state of the pavement report
    edges_df['slope'] = np.where(edges_df['ispublicworks']==0, 0, edges_df['slope'])

    ### Set initial age as the current age
    edges_df['age_current'] = edges_df['initial_age'] ### age in days
    ### Current conditions
    edges_df['pci_current'] = edges_df['intercept'] + edges_df['slope'] * edges_df['age_current']/365
    edges_df['pci_current'] = np.where(edges_df['pci_current']>100, 100, edges_df['pci_current'])
    edges_df['pci_current'] = np.where(edges_df['pci_current']<0, 0, edges_df['pci_current'])
    print('total_blocks', len(np.unique(edges_df[edges_df['ispublicworks']==1]['cnn_expand'])))
    print('initial condition: ', np.mean(edges_df[(~edges_df['type'].isin(highway_type))&(edges_df['ispublicworks']==1)]['pci_current']))
    print('edges<63: ', sum(edges_df[(~edges_df['type'].isin(highway_type))&(edges_df['ispublicworks']==1)]['pci_current']<63))
    
    edges_df['juris'] = np.where(edges_df['ispublicworks']==1, 'DPW',
        np.where(edges_df['type'].isin(highway_type), 'Caltrans', 'no'))
    #edges_df.to_csv(absolute_path+'/{}/preprocessing.csv'.format(outdir), index=False)
    #sys.exit(0)

    return edges_df

def eco_incentivize(random_seed, budget, eco_route_ratio, iri_impact, case, traffic_growth, day, probe_ratio, total_years, improv_pct=1, closure_list=[], closure_case=''):

    ### Network preprocessing
    edges_df = preprocessing()
    step_results_list = []

    for year in range(total_years):
        gc.collect()

        ### Update current PCI
        edges_df['pci_current'] = edges_df['intercept'] + edges_df['slope']*edges_df['age_current']/365
        edges_df['pci_current'] = np.where(
            edges_df['pci_current']>100, 100, np.where(
                edges_df['pci_current']<0, 0, edges_df['pci_current']))
        
        ### Initialize the annual average daily
        aad_df = edges_df[['edge_id_igraph', 'length', 'type', 'slope_factor']].copy()
        aad_df = aad_df.assign(**{'aad_vol': 0, 'aad_vht': 0, 'aad_vmt': 0, 'aad_base_emi': 0})
        ### aad_vht: daily vehicle hours travelled
        ### aad_base_emi: emission not considering pavement degradations

        if (case in ['nr', 'em']) and (not traffic_growth):
            for hour in range(3, 27):
                hour_volume_df = pd.read_csv(absolute_path+'/{}/edges_df_singleyear/edges_df_DY{}_HR{}_r0_p1.csv'.format(outdir, day, hours))
                aad_df = aad_vol_vmt_baseemi(aad_df, hour_volume_df)

        elif (case in ['ee', 'er']) or traffic_growth:
            ### INITIAL GRAPH WEIGHTS: SPEED RELATED EMISSION
            ### Calculate the free flow speed in MPH, as required by the emission-speed model
            edges_df['ffs_mph'] = edges_df['length']/edges_df['fft']*2.23964
            ### FFS_MPH --> speed related emission
            edges_df['base_co2_ffs'] = base_co2(edges_df['ffs_mph']) ### link-level co2 eimission in gram per mile per vehicle
            ### Adjust emission by considering the impact of pavement degradation
            edges_df['pci_co2_ffs'] = edges_df['base_co2_ffs']*(1+0.0714*iri_impact*(100-edges_df['pci_current'])) ### emission in gram per mile per vehicle
            edges_df['eco_wgh'] = edges_df['pci_co2_ffs']/1609.34*edges_df['length']

            ### Output network graph for ABM simulation
            ### Shape of the network as a sparse matrix
            g_time = sio.mmread(absolute_path+'/../0_network/data/{}/{}/network_sparse.mtx'.format(folder, scenario))
            g_time_shape = g_time.shape
            wgh = edges_df['eco_wgh']
            row = edges_df['start_sp']-1
            col = edges_df['end_sp']-1
            g_eco = scipy.sparse.coo_matrix((wgh, (row, col)), shape=g_time_shape)
            sio.mmwrite(absolute_path+'/{}/network/network_sparse_r{}_b{}_e{}_i{}_c{}_tg{}_y{}.mtx'.format(outdir, random_seed, budget, eco_route_ratio, iri_impact, case, traffic_growth, year), g_eco)

            ### Output edge attributes for ABM simulation
            abm_edges_df = edges_df[['edge_id_igraph', 'start_sp', 'end_sp', 'slope_factor', 'length', 'capacity', 'fft', 'pci_current', 'eco_wgh']].copy()

            ### Run ABM
            abm_hour_volume_dict = sf_abm.sta(outdir, abm_edges_df, year=year, day=day, random_seed=random_seed, probe_ratio=probe_ratio, budget=budget, eco_route_ratio=eco_route_ratio, iri_impact=iri_impact, case=case, traffic_growth=traffic_growth, closure_list=closure_list, closure_case=closure_case)

            for hour in range(3, 27):
                read_case = case
                if len(closure_case)>0: 
                    read_case = closure_case
                hour_volume_df = abm_hour_volume_dict['hour_{}'.format(hour)]
                aad_df = aad_vol_vmt_baseemi(aad_df, hour_volume_df)

        else:
            print('no such case')

        ### Get pci adjusted emission
        aad_df = pd.merge(aad_df, edges_df[['edge_id_igraph', 'cnn_expand', 'ispublicworks', 'pci_current', 'intercept', 'slope', 'age_current']], on='edge_id_igraph', how='left')

        ### Adjust emission by considering the impact of pavement degradation
        aad_df['aad_pci_emi'] = aad_df['aad_base_emi']*(1+0.0714*iri_impact*(100-aad_df['pci_current'])) ### daily emission (aad) in gram

        aad_df['aad_emi_potential'] = aad_df['aad_base_emi']*(0.0714*iri_impact*(improv_pct * (100 - aad_df['pci_current'])))

        def pci_improvement(df, year, case, budget, eco_route_ratio, iri_impact): ### repair worst roads
            repair_df = df[(~df['type'].isin(highway_type)) & (df['ispublicworks']==1)].copy()
            repair_df = repair_df.groupby(['cnn_expand']).agg({'pci_current': np.mean}).reset_index().nsmallest(budget, 'pci_current')
            repair_list = repair_df['cnn_expand'].tolist()
            # extract_df = df.loc[df['cnn_expand'].isin(repair_list)]
            # extract_df[['edge_id_igraph', 'intercept', 'pci_current']].to_csv(absolute_path+'/{}/repair_df/repair_df_y{}_c{}_b{}_e{}_i{}.csv'.format(outdir, year, case, budget, eco_route_ratio, iri_impact), index=False)
            return repair_list

        def eco_maintenance(df, year, case, budget, eco_route_ratio, iri_impact):
            repair_df = df[(~df['type'].isin(highway_type)) & (df['ispublicworks']==1)].copy()
            repair_df = repair_df.groupby(['cnn_expand']).agg({'aad_emi_potential': np.sum}).reset_index().nlargest(budget, 'aad_emi_potential')
            repair_list = repair_df['cnn_expand'].tolist()
            # extract_df = df.loc[df['cnn_expand'].isin(repair_list)]
            #extract_df[['edge_id_igraph', 'aad_emi_potential']].to_csv('repair_df/repair_df_y{}_c{}_b{}_e{}_i{}.csv'.format(year, case, budget, eco_route_ratio, iri_impact))
            return repair_list

        if case in ['nr', 'er']: 
            repair_list = pci_improvement(aad_df, year, case, budget, eco_route_ratio, iri_impact)
            ### Repair
            edges_df['age_current'] = edges_df['age_current']+365
            edges_df['intercept'] = np.where(edges_df['cnn_expand'].isin(repair_list),
                edges_df['intercept'] + improv_pct*(100-edges_df['pci_current']),
                edges_df['intercept'])

        elif case in ['em', 'ee']:
            repair_list = eco_maintenance(aad_df, year, case, budget, eco_route_ratio, iri_impact)
            ### Repair
            edges_df['age_current'] = edges_df['age_current']+365
            edges_df['intercept'] = np.where(edges_df['cnn_expand'].isin(repair_list),
                edges_df['intercept'] + improv_pct*(100-edges_df['pci_current']),
                edges_df['intercept'])
        else:
            print('no matching maintenance strategy')

        ### Results
        ### emi
        emi_total = np.sum(aad_df['aad_pci_emi'])/1e6 ### co2 emission in t
        emi_local = np.sum(aad_df[aad_df['ispublicworks']==1]['aad_pci_emi'])/1e6
        emi_highway = np.sum(aad_df[aad_df['type'].isin(highway_type)]['aad_pci_emi'])/1e6
        emi_localroads_base = np.sum(aad_df[aad_df['ispublicworks']==1]['aad_base_emi'])/1e6

        ### vht
        vht_total = np.sum(aad_df['aad_vht']) ### vehicle hours travelled
        vht_local = np.sum(aad_df[aad_df['ispublicworks']==1]['aad_vht'])
        vht_highway = np.sum(aad_df[aad_df['type'].isin(highway_type)]['aad_vht'])
        ### vkmt
        vkmt_total = np.sum(aad_df['aad_vmt'])/1000 ### vehicle kilometers travelled
        vkmt_local = np.sum(aad_df[aad_df['ispublicworks']==1]['aad_vmt'])/1000
        vkmt_highway = np.sum(aad_df[aad_df['type'].isin(highway_type)]['aad_vmt'])/1000
        ### pci
        pci_average = np.mean(aad_df['pci_current'])
        pci_local = np.mean(aad_df[aad_df['ispublicworks']==1]['pci_current'])
        pci_highway = np.mean(aad_df[aad_df['type'].isin(highway_type)]['pci_current'])

        step_results_list.append([random_seed, case, budget, iri_impact, eco_route_ratio, year, emi_total, emi_local, emi_highway, emi_localroads_base, pci_average, pci_local, pci_highway, vht_total, vht_local, vht_highway, vkmt_total, vkmt_local, vkmt_highway])
    #print(step_results_list[0:10:9])
    return step_results_list

def degradation_model_sensitivity():
    
    budget = 700
    eco_route_ratio = 0
    iri_impact = 0.03

    ### Sensitivity parameters
    offset_list = [True] # [True, False] or [True] ### whether to offset intial value to 74
    improv_pct_list = [1] # [1, 0.75, 0.5] or [1] ### maintenance gains
    slope_mlt_list = [1,3,5] # [1, 3, 5] or [1] ### degradation rates

    results_list = []
    for case in ['normal', 'eco']:
        for improv_pct in improv_pct_list:
            for slope_mlt in slope_mlt_list:
                for offset in offset_list:

                    edges_df0 = preprocessing(offset=offset) 
                    edges_df = edges_df0.copy()
                    edges_df['slope'] *= slope_mlt
                    edges_df['age_current'] /= slope_mlt
                    step_results_list = eco_incentivize(edges_df, budget, eco_route_ratio, iri_impact, case, improv_pct=improv_pct)
                    [year_result.extend([improv_pct, slope_mlt, offset]) for year_result in step_results_list]
                    results_list += step_results_list


    results_df = pd.DataFrame(results_list, columns=['case', 'budget', 'iri_impact', 'eco_route_ratio', 'year', 'emi_total', 'emi_local', 'emi_highway', 'emi_localroads_base', 'pci_average', 'pci_local', 'pci_highway', 'vht_total', 'vht_local', 'vht_highway', 'vkmt_total', 'vkmt_local', 'vkmt_highway', 'improv_pct', 'slope_mlt', 'offset'])
    print(results_df.shape)
    results_df.to_csv('{}/results/scen12_results_model_sensitivity_slope_mlt.csv'.format(outdir), index=False)

def closure_analysis():
    
    ### edge_id_igraph of local roads with highest, mean and 25% bidirectional volumes on Friday
    closure_dict = {'normal': [], 'max': [6089, 26873], 'mean': [20049, 20053], 'low_quant': [6785, 8192]}

    budget = 0
    eco_route_ratio = 0
    iri_impact = 0.03
    case = 'er' ### eco-routing with 0 percent eco-routing vehicles to invoke the abm.
    improv_pct = 0
    edges_df0 = preprocessing()

    day = 4
    total_years = 1

    for key, value in closure_dict.items():
        edges_df = edges_df0.copy()
        results_list = eco_incentivize(edges_df, budget, eco_route_ratio, iri_impact, case, improv_pct=improv_pct, closure_list = value, closure_case = key)
        results_df = pd.DataFrame(results_list, columns=['case', 'budget', 'iri_impact', 'eco_route_ratio', 'year', 'emi_total', 'emi_local', 'emi_highway', 'emi_localroads_base', 'pci_average', 'pci_local', 'pci_highway', 'vht_total', 'vht_local', 'vht_highway', 'vkmt_total', 'vkmt_local', 'vkmt_highway'])
        results_df.to_csv(absolute_path+'/{}/results/closure_{}.csv'.format(outdir, key), index=False)


def scenarios():

    ### Emission analysis parameters
    random_seed = 0#int(os.environ['RANDOM_SEED']) ### 0,1,2,3,4,5,6,7,8,9
    budget = int(os.environ['BUDGET']) ### 200 or 700
    eco_route_ratio = float(os.environ['ECO_ROUTE_RATIO']) ### 0.1, 0.5 or 1
    iri_impact = float(os.environ['IRI_IMPACT']) ### 0.01 or 0.03
    case = os.environ['CASE'] ### 'nr' no eco-routing or eco-maintenance, 'em' for eco-maintenance, 'er' for 'routing_only', 'ee' for 'both'
    traffic_growth = int(os.environ['TRAFFIC_GROWTH']) ### 1 or 0
    print('random_seed {}, budget {}, eco_route_ratio {}, iri_impact {}, case {}, traffic_growth {}'.format(random_seed, budget, eco_route_ratio, iri_impact, case, traffic_growth))

    ### ABM parameters
    day = 2 ### Wednesday
    probe_ratio = 1

    ### simulation period
    total_years = 11

    step_results_list = eco_incentivize(random_seed, budget, eco_route_ratio, iri_impact, case, traffic_growth, day, probe_ratio, total_years)
    results_df = pd.DataFrame(step_results_list, columns=['random_seed', 'case', 'budget', 'iri_impact', 'eco_route_ratio', 'year', 'emi_total', 'emi_local', 'emi_highway', 'emi_localroads_base',  'pci_average', 'pci_local', 'pci_highway', 'vht_total', 'vht_local', 'vht_highway', 'vkmt_total', 'vkmt_local', 'vkmt_highway'])
    #print(results_df.iloc[-1])
    results_df.to_csv(absolute_path+'/{}/results/scen_res_r{}_b{}_e{}_i{}_c{}_tg{}.csv'.format(outdir, random_seed, budget, eco_route_ratio, iri_impact, case, traffic_growth), index=False)

if __name__ == '__main__':

    # preprocessing()
    # sys.exit(0)

    # exploratory_budget()
    # sys.exit(0)

    # eco_incentivize(1500, 0, 0.03, 'normal')
    # sys.exit(0)

    # degradation_model_sensitivity()
    # sys.exit(0)

    # closure_analysis()
    # sys.exit(0)

    scenarios()
    ### Running different eco-maintenance and eco-routing scenarios

