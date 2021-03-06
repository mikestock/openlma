#!/usr/bin/python
#
"""raw_io
Reads and writes various LMA data formats
Support inlcudes:
In:
  raw lma v8  (80us)
  raw lma v9  (10us)
  raw lma v10 (80us)
  raw lma v12 (80us)
"""

import struct, os, sys, time, warnings
import numpy as np
#TODO - change to relative imports
from common import *
from constants import *


frameDtype = [ ('nano', 'i'),
               ('power', 'f'),
               ('aboveThresh', 'i')]

class RawLMAFile:

    def __init__ (self, inputPath, decimated=False ):
        """
        inputPath = path to lma data file
        decimated = [bool] - set to True if reading decimated (rt) LMA data
        """

        #lat/lon information
        #we could get this from a location file as well, but it's contained 
        #in the V10+ data files
        self.gpslat = 0 #these guys need to be converted
        self.gpslon = 0
        self.gpsalt = 0
        self.lat = 0
        self.lon = 0
        self.alt = 0
        self.vel = 0
        self.brg = 0

        #timing information
        self.startEpoch = 0
        self.endEpoch   = 0

        self.dataVersion = None #there's a number of different LMA raw data versions
        self.inputPath   = inputPath
        self.decimated   = decimated
        #try opening the inputPath, that should work if it exists
        if os.path.exists( self.inputPath ):
            self.inputFile = open( self.inputPath, 'rb' )
            self.inputFileSize = os.path.getsize( self.inputPath )
        else:
            raise Exception( 'RawLMA.__init__: inputPath does not exist: %s'%self.inputPath )
        
        #we need to find the file locations of each of the status words
        #these will break up the file into 1 second chunks
        self.find_status()

        #setup search dicts
        self.make_frameEpochs( )

        #finalize location stuff
        if self.lat !=0 and self.lon != 0 and self.alt != 0:
            self.geodetic  = self.lat, self.lon, self.alt
            self.cartesian = latlonalt2xyz( *self.geodetic )
        else:
            #the location data was not set in the status words, 
            #or maybe it was set incompletely 
            self.geodetic  = None
            self.cartesian = None

    def make_frameEpochs(self):
        self.frameEpochs = {}
        for iFrame in range( 1, len(self.statusPackets) ):
            epoch = self.statusPackets[iFrame].epoch
            self.frameEpochs[ epoch ] = iFrame


    def convert_latlon( self, gpsInt):
        """
        this takes in the gps integer value used for latitude and longitude, and 
        converts it into decimal degrees
        """

        #is the number negative?
        if gpsInt >> 31 == 1:
            #yes it is
            gpsInt -= 1<<32
        
        latlon = gpsInt *90/324000000.0
        return latlon

    def find_status( self ):
        self.statusLocations = []
        self.statusPackets   = []
        self.inputFile.seek( 0 )

        #the LMA raw data uses the first bit of the data words to make a pattern
        #the data packets have first bytes that go 0, 1, 0
        #the status packets have first bytes that go 1,1,1,1,1,1,1,1,1
        #so, we can struct to decode the data, and test if the numbers are 
        #positive or negative to determine if we've found a status word

        #the status packets do include how many data packets they include, 
        #but, the status comes at the end of the data.  We need to scan backwards 
        #from the end of the file, but to do that we need to know the data version
        #so we know how big a status packet is
        statusPacket = StatusPacket( self.inputFile.read(18) )
        self.version = statusPacket.version
        self.id      = statusPacket.id
        self.netid   = statusPacket.netid
        #this is the first status message, there are no frames associated with it
        #but we can use it to get the starting epoch of the file
        self.startEpoch = statusPacket.epoch+1  
        if self.version >= 10:
            self.statusSize = 18
        else:
            self.statusSize = 12

        if self.decimated:
            #this is way slower
            self._search_forwards()
        else:
            #this is faster
            self._search_backwards()

    def _search_forwards( self ):
        self.statusLocations.append( 0 )
        self.statusPackets.append( None )   #the first status shouldn't be used

        fileLocation = self.statusSize
        while fileLocation < os.path.getsize( self.inputPath ):
            self.inputFile.seek( fileLocation )
            try:
                statusPacket = StatusPacket( self.inputFile.read(self.statusSize) )
                if statusPacket.id != self.id or statusPacket.netid != self.netid:
                    #well that's funny, these should be the same for all status packets in the file
                    raise Exception( 'RawLMAFile._searchForwards : statusPacket id not consistent in file')
                
                #update ending epoch
                if statusPacket.epoch > self.endEpoch: 
                    #the statusPackets are time ordered, and we're searching forward.
                    #this should always be true
                    self.endEpoch = statusPacket.epoch

                #if we could read the statusPacket, we're in the right spot
                self.statusLocations.append( fileLocation )
                self.statusPackets.append( statusPacket )

                #GPS Stuff
                self. decode_gpsInfo( statusPacket )

                fileLocation += self.statusSize
            
            except:
                #means that the bit pattern was wrong
                fileLocation += 3

    def _search_backwards(self):
        #now search for the remaining status packets in reverse, start by 
        #seeking to the end of the file
        self.inputFile.seek(0, 2)
        while self.inputFile.tell() > self.statusSize:
            self.inputFile.seek( -self.statusSize, 1 )
            self.statusLocations.append( self.inputFile.tell() )
            statusPacket = StatusPacket( self.inputFile.read(self.statusSize) )
            self.statusPackets.append( statusPacket )

            if statusPacket.id != self.id or statusPacket.netid != self.netid:
                #well that's funny, these should be the same for all status packets in the file
                raise Exception( 'RawLMAFile._searchForwards : statusPacket id not consistent in file')

            #update ending epoch
            if statusPacket.epoch > self.endEpoch: 
                #the statusPackets are time ordered, and we're searching backwards.
                #this should only be true once
                self.endEpoch = statusPacket.epoch

            #GPS Stuff
            self. decode_gpsInfo( statusPacket )

            #determine how far back to seek
            if self.inputFile.tell() > statusPacket.triggerCount * 6 + self.statusSize:
                self.inputFile.seek( -statusPacket.triggerCount * 6 -self.statusSize, 1)
            else:
                #this really shouldn't happen
                #because the raw file should start with a status
                #(and we used that fact to get the version)
                warnings.warn( "RawLMA.find_status encountered condition which shouldn't happen" )
                break

        #we didn't add the first status packet to the list, and that's ok 
        #because there are no data packets associated with it (they're in the previous file)
        #but, we would like to add in the location, to make later math a little easier
        self.statusLocations.append( 0 )
        self.statusPackets.append( None )   #the first status shouldn't be used

        #the information on the status packets is reversed
        self.statusLocations.reverse()
        self.statusPackets.reverse()

    def decode_gpsInfo( self, statusPacket ):
            #handle GPS info
            if statusPacket.second %12 ==0:
                #lat bytes 4/3
                self.gpslat = (statusPacket.gpsInfo<<16) | (self.gpslat &0xFFFF)
                self.lat = self.convert_latlon( self.gpslat )
            elif statusPacket.second %12 ==1:
                #lat bytes 2/1
                self.gpslat = (statusPacket.gpsInfo) | (self.gpslat &0xFFFF0000)
                self.lat = self.convert_latlon( self.gpslat )
            elif statusPacket.second %12 ==2:
                #lon bytes 4/3
                self.gpslon = (statusPacket.gpsInfo<<16) | (self.gpslon &0xFFFF)
                self.lon = self.convert_latlon( self.gpslon )
            elif statusPacket.second %12 ==3:
                #lon bytes 2/1
                # self.gpslon = (statusPacket.gpsInfo<<16) | (self.gpslon &0xFFFF)
                self.gpslon = (statusPacket.gpsInfo) | (self.gpslon &0xFFFF0000)
                self.lon = self.convert_latlon( self.gpslon )
            elif statusPacket.second %12 ==4:
                #alt bytes 4/3
                self.gpsalt = (statusPacket.gpsInfo<<16) | (self.gpsalt &0xFFFF)
                self.alt = self.gpsalt/100.0
            elif statusPacket.second %12 ==5:
                #alt bytes 2/1
                self.gpsalt = (statusPacket.gpsInfo) | (self.gpsalt &0xFFFF0000)
                self.alt = self.gpsalt/100.0
            elif statusPacket.second %12 ==6:
                #vel bytes 4/3
                self.vel = (statusPacket.gpsInfo<<16) | (self.vel &0xFFFF)
            elif statusPacket.second %12 ==7:
                #vel bytes 2/1
                self.vel = (statusPacket.gpsInfo) | (self.vel &0xFFFF0000)
            elif statusPacket.second %12 ==8:
                #brg 
                self.brg = statusPacket.gpsInfo
            elif statusPacket.second %12 ==9:
                #vis/tracked satellites
                self.satTracked = (statusPacket.gpsInfo>>8) &0xFF
                self.satVisible = statusPacket.gpsInfo &0xFF
            elif statusPacket.second %12 ==10:
                #statellite stat?
                self.satStat = statusPacket.gpsInfo &0xFFF
            elif statusPacket.second %12 ==11:
                #temperature 
                self.temp = (statusPacket.gpsInfo>>8)-40

    def read_frame( self, iStatus):
        """
        Read in all the datapackets associated with the ith status message

        returns a structured numpy array
        """
        ###
        # Profiling indicates that the bottle neck for reading data is in the decoding
        # rather than in the reading.  If it were the read, we could probably improve 
        # things by reading larger blocks all at once
        # The decoding is all the bit-shift stuff that happens because of the uniquely 
        # anoying way that Rison has split data between words in the file.  
        # Speeding this up will require moving the decode methods over to Cython
        # Current performance benchmark: 1 ten minute file is read and decoded in about 4 seconds

        if iStatus <= 0 or iStatus >= len(self.statusLocations) :
            raise Exception( "Can't read the %ith collection of data, choose number between 1 and %i"%(iStatus, len(self.statusLocations)-1) )


        #we get the start and end point of our read from the statusLocations
        #those are file locations.  We need to offset the start point by the 
        #size of 1 statusPacket though
        readStart = self.statusLocations[ iStatus-1 ]+self.statusSize
        readEnd   = self.statusLocations[ iStatus ]

        #Get some information we'll need from the assoicated status packet
        statusPacket = self.statusPackets[ iStatus ]
        #set status GPS information
        statusPacket.geodetic = self.geodetic
        statusPacket.cartesian = self.cartesian

        #get other things from the status packet
        version   = statusPacket.version    #needed for dataPacket format
        phaseDiff = statusPacket.phaseDiff  #only if we want good timinh
        if not self.decimated:
            triggerCount= statusPacket.triggerCount #this is how many dataPackets there will be
        else:
            #we can't trust the triggerCount for decimated data
            triggerCount = int( (readEnd-readStart)/6 )

        #data is going into this thing.  
        #the structured type is needlessly fancy.  Could be worse and be a pandas dataframe thing
        dataArray = np.zeros( triggerCount, dtype=frameDtype)

        self.inputFile.seek( readStart )
        for i in range( triggerCount ):
            if self.inputFile.tell() >= readEnd:
                raise Exception( "RawLMA.read - data packet reading is out of bounds, %i>=%i"%(self.inputFile.tell(), readEnd))
            d = DataPacket( self.inputFile.read(6), version=version, phaseDiff=phaseDiff )
            
            dataArray['nano'][i]        = d.nano
            dataArray['power'][i]       = d.power
            dataArray['aboveThresh'][i] = d.aboveThresh
        
        return LMAFrame( statusPacket, inputArray=dataArray )

class LMAFrame( ):

    def __init__( self, statusPacket, inputArray=None ):
        self.statusPacket = statusPacket

        #copy a bunch of the statusPacket attributes over
        #I think there's probably a better way to do this, with inheritence or somth
        #but am too dumb to know how
        self.geodetic  = statusPacket.geodetic
        self.cartesian = statusPacket.cartesian
        self.epoch     = statusPacket.epoch
        self.id        = statusPacket.id
        self.netid     = statusPacket.netid

        if self.epoch <= 0:
            #something has gone wrong
            warnings.warn( 'LMAFrame - epoch for frame set to value before LMA was invented')
        
        #protype attributes
        self.nano = None
        self.power = None
        self.aboveThresh = None

        #the defaults to None if no inputArray given
        self._arr = inputArray

        self.update()
    
    def append( self, nano, power, aboveThresh, update=True ):
        """
        """
        #initialize underlying array
        if self._arr is None:
            self._arr = np.empty( 0, dtype=frameDtype )
        
        #appending is done by extending the array 1 element, and 
        #then shoving the new data in at the end.  
        #doing it this way does not require the _arr to be copied
        N = len( self._arr )
        self._arr.resize( (N+1,), refcheck=False )
        self._arr[N] = nano,power,aboveThresh

        if update: self.update()

    def copy( self, inplace=True ):
        """
        Because of the opaque and fun way python and numpy handle pointers, 
        sometimes you don't have a copy of stuff in memory when you think you do
        Use this method to force the issue.  Can be done in-place
        """
        if inplace:
            self._arr = self._arr.copy()
            self.update()
        else:
            frame = LMAFrame( self.statusPacket, inputArray=self._arr.copy() )
            return frame

    def decimate( self, windowLength ):
        _arr = np.empty( 0, dtype=frameDtype )

        nano = 0
        i = 0
        N = 0
        while nano+windowLength < 1e9 and i < len( self._arr ):
            #skip forward?  Data is unevenly distributed
            if self._arr['nano'][i] >= nano+windowLength:
                nano += windowLength
                continue
            #inside this window, find the max sample
            maxPeak = self._arr[i]
            while self._arr['nano'][i] < nano+windowLength:
                #we keep the highest power peak each time
                if self._arr['power'][i] > maxPeak['power']:
                    maxPeak = self._arr[i]
                i += 1
                #for the last window, we might run off the end of the array
                #don't
                if i >= len( self._arr ): break
            #append the maxPeak to the new array
            _arr.resize( (N+1,), refcheck=False )
            _arr[N] = maxPeak
            N += 1

        #apply the new _arr to self, this destroys the old _arr
        self._arr = _arr
        self.update()

    def update( self ):
        """
        """
        #make sure we have something to update
        if self._arr is None: return
        self.nano        = self._arr['nano'][:]
        self.power       = self._arr['power'][:]
        self.aboveThresh = self._arr['aboveThresh'][:]

class StatusPacket:

    #There are a lot of masks used in doing this the Rison way
    #Here's a cheat sheet
    #0x1  0001
    #0x3  0011
    #0x7  0111
    #0xF  1111
    #0x8  1000
    #0x4  0100
    #0x2  0010
    #0x1  0001

    def __init__( self, inputString ):

        self.inputString = inputString
        #decode the words
        self.words = struct.unpack( '<9h', inputString )

        #v8/9 status messages only have 6 words
        #v10+ status messages have 9 words to add in phase and gps information
        #get the version, we do this with masks and fun stuff
        #note: the spec for v8/9 says 6 bits are used for version, and 1 for phase count
        #      the spec for v12  says 7 bits are used for version
        #this is bad, it means we can't be sure we've decoded the version correctly.
        #the issue isn't massive, phase_count is unlikely to get big for v8/9 because 
        #it still has the phase locked loop in place, and the msb for version won't 
        #flip to 1 until version 64 (and we're on version 13 now).  It'll be some 
        #years before this is a problem.  For now, we're going to only decode a 6bit 
        #version number, since that's safest.  
        self.version = (self.words[0]>>7) &0x3f 
        if self.version < 10:
            self.words = self.words[:6]
        
        #Attribute prototyping
        self.year         =  0
        self.month        =  0
        self.day          =  0
        self.hour         =  0
        self.minute       =  0
        self.second       =  0
        self.epoch        =  0  #hopefully it doesn't stay 0
        self.threshold    =  None
        self.fifoStatus   =  None
        self.phaseDiff    =  None
        self.triggerCount =  None
        self.id           =  None
        self.netid        =  None
        self.track        =  None

        #these need to get set manually
        self.geodetic     =  None
        self.cartesian    =  None

        #we've already decoded the version number, if this fails the version 
        #was probably complete trash.  Oh well
        if not all( [v<0 for v in self.words] ):
            raise Exception( "Malformed status packet doesn't follow bit pattern" )

        self.decode()
        self.calc_epoch()

    def calc_epoch( self ):
        #it's surprisingly annoying converting from numerical values for
        #year/month/day into an epoch
        #the easiest way to do it is to convert to a string, and then back
        #because of course that's what you need to do
        timestamp = '%i%02i%02iT%02i:%02i:%02i'%(self.year, self.month, self.day, self.hour, self.minute, self.second)
        self.epoch = timestamp2epoch( timestamp )

    def decode( self ):
        #the various decode methods all look very similar, since changes 
        #in the data format happened gradually over time.  That means there 
        #a bunch of copy-paste code lying around, but in this case I think 
        #that's for the better, as it issolates the decoding used for each 
        #version, without muddying the situation with share methods for the 
        #sake of sharing.  
        if self.version == 10 or self.version == 11:
            self.decode_1011()
        elif self.version == 12 or self.version == 13:
            self.decode_1213()
        else:
            raise Exception( 'Unknown raw data version %i'%self.version )

    def decode_89( self ):
        #reference data_format_v8_revised.pdf
        self.year         = (self.words[0] &0x7F) + 2000
        self.threshold    =  self.words[1] &0xFF
        self.fifoStatus   = (self.words[2]>>12)&0x07
        self.second       = (self.words[2]>>6 )&0x3F
        self.minute       =  self.words[2] &0x3F
        self.hour         = (self.words[3]>>9)&0x1F
        self.day          = (self.words[3]>>4)&0x1F
        self.month        =  self.words[3] &0x0F
        #phaseDiff in this is called 'phase count' and is different
        #note, there is another bit of this, but I'm assuming it's always 0
        self.phaseDiff    = (self.words[1]>>8)&0x1F
        #sign of phaseDiff
        if (self.words[1]>>14)%2 == 1:
            self.phaseDiff *= -1
        self.triggerCount = (self.words[5]&0x1FF) | (self.words[4]&0x7F)<<9 
        #the ID ought to be a char but I'm not sure how Rison excoded it
        #will need example file to sort it out
        #TODO - sort out character encoding
        self.id           = (self.words[4]>>8)&0x7F
        self.track        = (self.words[5]>>12)&0xF  #I'm not sure what this is

    def decode_1011( self ):
        #reference data_format_v12.pdf
        #but with the network ID portion removed
        #which is why this is so similar to decode_1213
        self.year         = (self.words[0] &0x7F) + 2000
        self.threshold    =  self.words[1] &0xFF
        self.fifoStatus   = (self.words[2]>>12)&0x07
        self.second       = (self.words[2]>>6 )&0x3F
        self.minute       =  self.words[2] &0x3F
        self.hour         = (self.words[3]>>9)&0x1F
        self.day          = (self.words[3]>>4)&0x1F
        self.month        =  self.words[3] &0x0F
        self.triggerCount =  self.words[4] &0x3FFF
        self.id           = (self.words[1]>>5)&0x80 | (self.words[5]>>8)&0x7F
        #in typical Bill fashion, he's offset the ID by 64, to skip the non-letter values
        #in less Bill fashion, he used 8 bits to encode, even though ASCII only has 128 values
        #(even less if you skip the first 64 characters)
        self.id           = chr(self.id+64)
        self.netid        = ''
        self.phaseDiff    =  self.words[6] &0x7FFF
        #sign bit stored elsewhere
        if (self.words[1]>>14)&0x1 == 1:
            self.phaseDiff *= -1
        
        ###
        # GPS info is updated on a 12 second cycle
        # TODO - handle the 12 second cycle
        # self.gpsInfo      = (self.words[7] &0x7FFF) | (self.words[1]<<2)&0x8000
        self.gpsInfo      = (self.words[7] &0x7FFF) | (self.words[1]&0x2000)<<2

    def decode_1213( self ):
        #reference data_format_v12.pdf
        self.year         = (self.words[0] &0x7F) + 2000
        self.threshold    =  self.words[1] &0xFF
        self.fifoStatus   = (self.words[2]>>12)&0x07
        self.second       = (self.words[2]>>6 )&0x3F
        self.minute       =  self.words[2] &0x3F
        self.hour         = (self.words[3]>>9)&0x1F
        self.day          = (self.words[3]>>4)&0x1F
        self.month        =  self.words[3] &0x0F
        self.triggerCount =  self.words[4] &0x3FFF
        self.id           = (self.words[1]>>5)&0x80 | (self.words[5]>>8)&0x7F
        self.netid        =  self.words[5] & 0x00FF
        #in typical Bill fashion, he's offset the ID by 64, to skip the non-letter values
        #in less Bill fashion, he used 8 bits to encode, even though ASCII only has 128 values
        #(even less if you skip the first 64 characters)
        self.id           = chr(self.id+64)
        self.netid        = chr(self.netid+64)
        self.phaseDiff    =  self.words[6] &0x7FFF
        #sign bit stored elsewhere
        if (self.words[1]>>14)&0x1 == 1:
            self.phaseDiff *= -1
        
        ###
        # GPS info is updated on a 12 second cycle
        # TODO - handle the 12 second cycle
        # self.gpsInfo      = (self.words[7] &0x7FFF) | (self.words[1]<<2)&0x8000
        self.gpsInfo      = (self.words[7] &0x7FFF) | (self.words[1]&0x2000)<<2

class DataPacket:

    #There are a lot of masks used in doing this the Rison way
    #Here's a cheat sheet
    #0x1  0001
    #0x3  0011
    #0x7  0111
    #0xF  1111
    #0x8  1000
    #0x4  0100
    #0x2  0010
    #0x1  0001

    def __init__(self, inputString, version, phaseDiff=0 ):
        self.inputString = inputString
        self.version     = version
        self.phaseDiff   = phaseDiff
        #decode the words
        self.words = struct.unpack( '<3h', inputString )

        #the data packet should be +, -, +
        pattern = [v<0 for v in self.words]
        if not pattern == [False, True, False]:
            raise Exception( "Malformed data packet doesn't follow bit pattern" )
    
        self.decode()
    

    def decode( self ):
        """
        Yeah, this is just a mapping between version numbers, and decode 
        implementations.
        """
        if self.version == 12 or self.version==10:
            self.decode_12()

    def decode_8( self ):
        #even version numbers are for 80us
        #the us field is actually the window number, wider windows means less windows/second
        #means this field needs less bits
        #the ns field is the number of ticks the peak happens in the window.  Longer windows 
        #means this number is bigger and needs more bits

        #version 8 firmware still had the phase locked loop
        samplePeriod = 1e9/( 25000000 )    #in ns
        windowLength = 80000    #80us

        #v8 and v12 are almost the same, except in the older data, 
        #aboveThreshold is split between 3 words instead of 2
        #TODO - verify that I have the middle shift right
        self.aboveThresh = (self.words[0] >> 11) | ((self.words[1]&0x4000)>>10) | ((self.words[2]&0x7F00)>>4)
        self.ticks       = (self.words[0] & 0x07FF)  #called nano by WR
        self.window      = (self.words[1] & 0x3FFF)  #called micro by WR
        self.maxData     = (self.words[2] & 0x00FF)
        
        #use the window number and ticks to get the actual time of this event
        #accurate to 1 ns
        self.nano        = self.window*windowLength + int( self.ticks*samplePeriod )
        #convert maxData to power in dBm
        self.power       = 0.488*self.maxData -111.0

    def decode_9( self ):
        #odd version numbers are for 10us
        #the us field is actually the window number, narrower windows means more windows/second
        #means this field needs more bits
        #the ns field is the number of ticks the peak happens in the window.  Narrower windows 
        #means this number is smaller and needs less bits

        #version 9 firmware still had the phase locked loop
        samplePeriod = 1e9/( 25000000 )    #in ns
        windowLength = 10000    #80us

        #v8 and v12 are almost the same, except in the older data, 
        #aboveThreshold is split between 3 words instead of 2
        #TODO - verify that I have the middle shift right
        self.aboveThresh = (self.words[0] >> 11) | ((self.words[1]&0x4000)>>10) | ((self.words[2]&0x7F00)>>4)
        self.ticks       = (self.words[0] & 0x00FF)  #called nano by WR
        #TODO - verifty that I have the window shift right, this one is important
        self.window      = (self.words[1] & 0x3FFF) | (self.words[0] &0x0700)<<6 #called micro by WR
        self.maxData     = (self.words[2] & 0x00FF)
        
        #use the window number and ticks to get the actual time of this event
        #accurate to 1 ns
        self.nano        = self.window*windowLength + int( self.ticks*samplePeriod )
        #convert maxData to power in dBm
        self.power       = 0.488*self.maxData -111.0

    def decode_12( self ):
        #even version numbers are for 80us
        #the us field is actually the window number, wider windows means less windows/second
        #means this field needs less bits
        #the ns field is the number of ticks the peak happens in the window.  Longer windows 
        #means this number is bigger and needs more bits
        samplePeriod = 1e9/( 25000000 + self.phaseDiff )    #in ns
        windowLength = 80000    #80us

        self.aboveThresh = (self.words[0] >> 11) | ( (self.words[2]&0xFF00)>>4 )
        self.ticks       = (self.words[0] & 0x07FF)  #called nano by WR
        self.window      = (self.words[1] & 0x3FFF)  #called micro by WR
        self.maxData     = (self.words[2] & 0x00FF)
        
        #use the window number and ticks to get the actual time of this event
        #accurate to 1 ns
        self.nano        = self.window*windowLength + int( self.ticks*samplePeriod )
        #convert maxData to power in dBm
        self.power       = 0.488*self.maxData -111.0

class Station:
    """
    Holder for information about a station or network
    """
    def __init__( self, name=None, id=None, geodetic=None, cartesian=None, delay=None, boardVersion=None, channel=None):
        self.name = name
        self.id = id
        self.geodetic = geodetic
        self.cartesian = cartesian
        self.delay = delay
        self.boardVersion = boardVersion
        self.channel = channel

class LocFile:
    """
    The location file for LMA data stores information on the location, and names of all the sensors
    We can also get this location inforation from the raw data packets (dataVersion >= 10)
    But, the data files do not include information about the cable delays.  And when processing 
    RT data, sometimes there is not sufficient number of seconds in the file to allow decoding 
    all location parameters (requires 12 seconds of data), and for older stations, the location 
    of the GPS and the location of the antenna may not be the same.  

    TLDR - even though the location information is store in the raw data, we still need a locFile

    Format is serial, 1 parameter per line:
    NetworkName <string>
    NetworkCenterLat
    NetworkCenterLon
    NetworkCenterAlt (usually approximate)

    StationName <string, descriptive>
    StationID   <string, 1 character.  Lowercase indicates 10us data, uppercase 80us data>
    StationLat
    StationLon
    StationAlt
    StationDelays <ns, cable delay>
    StationLMABoardVersion
    StationReceiverChannel

    station information is repeated for each station.  
    Comments in the file are indicated with leading #
    Comments can happen for any line
    """
    def __init__( self, inputPath=None ):
        self.inputPath = inputPath 

        self.sensors = {}
        self.network = None

        if self.inputPath != None:
            self.read()
    
    def read(self, inputPath = None):
        #set the inputPath
        if inputPath==None:
            inputPath = self.inputPath
        if inputPath==None:
            #if it's still None, we have nothing to read
            raise Exception( 'LocFile.read - No inputPath to read from')
        filePointer = open( inputPath, 'r' )

        self._read_network_info( filePointer )

        #we need to read stationInfo in a loop
        self.sensors = {}
        while True:
            #doing this is a try block is pretty janky, but will probably work
            try:
                self._read_station_info( filePointer )
            except:
                #_read_station_info throws an exception if it hits EOF
                #TODO make this an eof exception, and just catch that
                break
        filePointer.close()

    def _read_network_info( self, filePointer ):
        lines = []
        while len(lines) < 4:
            l = filePointer.readline()
            if l == '':
                #we should at least have a \n in there, unless we've hit the EOF
                raise Exception( 'LocFile._read_network_info - hit EOF')
            l = l.strip()
            if l[0] == '#':
                continue
            #TODO did we hit EOF?
            lines.append( l )
        
        lat = float( lines[1] )
        lon = float( lines[2] )
        alt = float( lines[3] )
        #convert to cartesian coordinates
        x,y,z = latlonalt2xyz( lat,lon,alt )
        networkInfo = Station( name=lines[0], geodetic=(lat,lon,alt), cartesian=(x,y,z) )

        self.network = networkInfo

    def _read_station_info( self, filePointer ):
        lines = []
        while len(lines) < 8:
            l = filePointer.readline()
            if l == '':
                #we should at least have a \n in there, unless we've hit the EOF
                raise Exception( 'LocFile._read_station_info - hit EOF')
            l = l.strip()
            if l[0] == '#':
                continue
            #TODO did we hit EOF?
            lines.append( l )
        #serial format is:
        #name, id, lat, lon, alt, delay, boardVersion, channel
        lat     = float( lines[2] )
        lon     = float( lines[3] )
        alt     = float( lines[4] )
        delay   = float( lines[5] )
        boardVersion = int( lines[6] )
        channel = int( lines[7] )
        id      = lines[1]

        #convert to cartesian coordinates
        x,y,z = latlonalt2xyz( lat,lon,alt )

        stationInfo = Station( name=lines[0], id=id, geodetic=(lat,lon,alt), cartesian=(x,y,z), delay=delay, boardVersion=boardVersion, channel=channel)
        self.sensors[ id ] = stationInfo

    def add( self, station ):
        self.sensors[ station.id ] = station 

    def write(self, outputPath=None):
        #TODO - should implement this
        #set the inputPath
        if outputPath==None:
            #write to the same place we read from, this is actually dangerous and will 
            #probably cause problems in the future when dumb people use the code
            #I'm probably one of those dumb people
            outputPath = self.inputPath
        if outputPath==None:
            #if it's still None, we have nothing to write
            raise Exception( 'LocFile.write - No outputPath to write to')

if __name__ == '__main__':
    #do a quick test
    #these tests are in test_io right now, they may get moved in the future
    print ('check out test_io if you wanna test this stuff')
