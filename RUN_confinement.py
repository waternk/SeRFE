# imports
import confinement

# directory
dir = 'SC/'

# Inputs - fill in
network = dir + 'SC_network_1km.shp'  # name and extension of drainage network shapefile
valley = dir + 'SC_VB.shp'  # name and extension of floodplain/valley bottom shapefile

# run confinement model - do not modify anything below
inst = confinement.Confinement(network, valley, exag=0.04)
inst.confinement()
inst.update_area()
