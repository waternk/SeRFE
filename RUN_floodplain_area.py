# imports
import floodplain_area

# directory
dir = 'Piru/'

# Inputs - fill in
network = dir + 'Piru_network_1km.shp'  # name and extension of drainage network shapefile
floodplain = dir + 'Piru_VB.shp'  # name and extension of floodplain/valley bottom shapefile
lg_buf = 1500  # maximum valley bottom width in high drainage area portions of network
med_buf = 500  # maximum valley bottom width in medium drainage area portions of network
sm_buf = 50  # maximum valley bottom width in small drainage area portions of the network

# run model - do not alter
floodplain_area.extract_floodplain_area(network, floodplain, lg_buf, med_buf, sm_buf)