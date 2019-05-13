import sys
sys.path.append("..") # Adds higher directory to python modules path.
import requests, threading, trip, conf
from datetime import datetime

def execute(start_time, end_time, trips, increment = 20):
    """ fetch all vehicle positions from start_time to end_time (POSIX)
    increment: increment (in seconds) to send timestamp to API
    To modify this module for your input data, you need to modify the timestamp loop and the input data
     - timestamp loop: look at the <loop> section within this function
     - input data: look at the <input> section within the FetchVehiclePositions function    
    """
    # ------- initiate global variables --------
    global ended_trips; ended_trips = {'total':0, 'notseen': 0, 'changetrip': 0} # list of trips to send to database
    global fleet; fleet = {} 			# operating vehicles in the ( fleet vid -> trip_obj )
    global fleet_lock; fleet_lock = threading.Lock() 	# prevent simulataneous editing
    global last_update_timestamp; last_update_timestamp = 0 # initiate latest update timestamp for feeds (to check if feeds are being generated)
    timestamp = start_time
    
    # <loop>
    # For example, if you have GTFS-realtime files on disk that are labeled with timestamps, use this timestamp loop to load them in.
    while timestamp < end_time:
        # send vehicle_positions api, update fleet, and store ending trips to DB
        FetchVehiclePositions(timestamp, trips)
        # increase timestamp 
        timestamp = timestamp + increment
        # print status
        sys.stdout.write('\r' + '{no_fleet} in fleet, {ended_trips}, at {time} '.format(
                no_fleet = len(fleet), ended_trips = ended_trips, time = datetime.fromtimestamp(timestamp).strftime("%b %d %Y %H:%M:%S"))
                )
        sys.stdout.flush()
    # <loop> end

def FetchVehiclePositions(timestamp, trips):
    """send vehicle_positions api call at timestamp, update fleet, and store ending trips to DB"""
    # <input>
    # The main goal of this section is to create a json object named 'ResponseParse' that represents a GTFS-realtime feed.
    # For example, if your GTFS-realtime protocol buffer file is on disk, load it in by using the gtfs-realtime-bindings python library (https://github.com/MobilityData/gtfs-realtime-bindings/blob/master/python/google/transit/gtfs_realtime_pb2.py)
    # then convert to json by using the google.protobuf library (https://pypi.org/project/protobuf/)    
    URL = conf.conf['API_URL']
    agency = conf.conf['agency']
    try:
        APICall = (URL + "api/gtfsrt/vehicle_positions" +                                      
                   "?source=" + agency +
                   "&timestamp=" + repr(timestamp)
                   )
        Response = requests.get(APICall)
        ResponseParse = Response.json()
    except Exception as e:
        print('API problem: ' + e)
        return
    # response received, check if status is ok    
    if (ResponseParse['header']['status'] != 'OK') or ('entity' not in ResponseParse.keys()):
        print('problem with API call: ' + APICall)
        return
    # has the closest feed's timestamp changed? If not, skip
    global last_update_timestamp
    timestamp = ResponseParse['header']['timestamp']
    if timestamp == last_update_timestamp:
        return 
    else:
        last_update_timestamp = timestamp
    
    # <input> end
        
    # ----- update fleet------
    ending_trips = []
    global ended_trips; global fleet
    vehicles = [item['vehicle'] for item in ResponseParse['entity'] if 'trip' in item['vehicle']]
    vehicles = [v for v in vehicles if 'tripId' in v['trip']] # filter vehicles without tripid

    with fleet_lock:
        # check if any trip ended, there can be 2 reasons:
        vids = fleet.keys(); vids = list(vids)
        for vid in vids:
            # if we haven't seen the vehicle for more than 30 minutes
            # or if the tripId changed
            if timestamp - int(fleet[vid].last_seen) > 1800:
                ending_trips.append(fleet[vid]); del fleet[vid]
                ended_trips['notseen'] = ended_trips['notseen'] + 1 
                continue
            if any(vehicle['vehicle']['id'] == vid for vehicle in vehicles):
            # if any vehicle in vehicle_positions feed match vid:
                if [v['trip']['tripId'] for v in vehicles if v['vehicle']['id'] == vid][0] != fleet[vid].trip_id:
                    # if the trip ID for that vehicle changed:
                    ending_trips.append(fleet[vid]); del fleet[vid]
                    ended_trips['changetrip'] = ended_trips['changetrip'] + 1
                    
        for v in vehicles:
			# get values from json list
            vid = v['vehicle']['id'] # vehicle id            
            tripId = v['trip']['tripId'] # trip id            
            if not any(trips.trip_id == tripId): # if trip Id not found in trips table, skip
                print("no trip match trip_id = " + tripId + " at time stamp " + repr(timestamp))
                continue
            rid = trips.route_id[trips.trip_id == tripId].values[0] # route id            
            did = trips.direction_id[trips.trip_id == tripId].values[0]  # direction id
            lon, lat = v['position']['longitude'], v['position']['latitude']
            v_timestamp = float(v['timestamp'])
            
            if vid not in fleet: # add to fleet if we have not seen this vehicle
                fleet[vid] = trip.Trip.new(trip_id = tripId, block_id = 0,direction_id = did, route_id = rid, vehicle_id = vid, last_seen = v_timestamp)			
                fleet[vid].add_point(lon,lat,v_timestamp)				
				# done with this vehicle
                continue
            else:
                # we have a record for this vehicle, just add the new position
                fleet[vid].add_point(lon,lat,v_timestamp)
                # then update the time and sequence
                fleet[vid].last_seen = v_timestamp
                fleet[vid].seq += 1
    # release the fleet lock
    
    # store the trips which are ending
    for some_trip in ending_trips:
        if len(some_trip.vehicles) > 1:
            some_trip.save_overwrite()
    ended_trips['total'] = ended_trips['total'] + len(ending_trips)