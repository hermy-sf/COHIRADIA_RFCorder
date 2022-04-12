# -*- coding: utf-8 -*-
"""
Created on Fri Dec 24 10:15:44 2021

@author: scharfetter_admin
"""
import sys
import time
import os
import numpy as np
import math
import datetime as ndatetime
from datetime import datetime
from socket import socket, AF_INET, SOCK_STREAM
from struct import pack
from PyQt5 import QtWidgets, QtCore
from PyQt5.QtCore import QObject, QThread, pyqtSignal, QMutex
from PyQt5.QtWidgets import QApplication, QMainWindow, QMessageBox
from PyQt5.QtGui import QFont
import paramiko
import yaml
from QT_RFcorder_v0_6 import Ui_MainWindow as MyRecorder


class timer_worker(QObject):

    """_generates time signals for clock and recording timer_
    """
    SigTick = pyqtSignal()
    SigFinished = pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def tick(self):

        '''
        Purpose: send a signal every second
        Returns
        -------
        None.

        '''
        while True:
            time.sleep(1 - time.monotonic() % 1)
            self.SigTick.emit()

    def stoptick(self):
        self.SigFinished.emit()


class playrec_worker(QObject):
    '''
    Purpose: worker object for playback and reording thread
    Arguments: Dictionary with entries:
        "fileHandle": file reference for reading/writing
        "timescaler": scaling constant bytes/second
        "TEST": diagnostic parameter for testing mode without STEMLAB
        "modality": "play" for playback, "rec" for recording
    '''
    # worker class for data streaming thread from PC to STEMLAB
    SigFinished = pyqtSignal()
    SigIncrementCurTime = pyqtSignal()
    SigBufferUnderflow = pyqtSignal()

    def __init__(self, sdr_params, *args, **kwargs):

        self.fileHandle = sdr_params.pop("fileHandle")
        self.timescaler = sdr_params.pop("timescaler")
        self.TEST = sdr_params.pop("TEST")
        self.modality = sdr_params.pop("modality")
        super().__init__(*args, **kwargs)
        self.stopix = False
        self.pausestate = False
        self.JUNKSIZE = 2048*4
        self.DATABLOCKSIZE = 1024*4
        self.RECSEC = self.timescaler*2  # number of bytes/s sent by sdr
        self.mutex = QMutex()

    def play_loop(self):        
        '''
        Porpose: worker loop for sending data to STEMLAB server

        Returns
        -------
        None.

        '''
        self.stopix = False
        position = 1
        self.fileHandle.seek(self.JUNKSIZE*position, 1)
        data = np.empty(self.DATABLOCKSIZE, dtype=np.int16)
        size = self.fileHandle.readinto(data)
        self.junkspersecond = self.timescaler / self.JUNKSIZE
        self.count = 0
        # print(f"Junkspersec:{self.junkspersecond}")
        while size > 0 and self.stopix is False:

            if self.TEST is False:
                if self.pausestate is False:
                    stemlabcontrol.data_sock.send(
                                            data[0:size].astype(np.float32)
                                            / 32767)  # send next 4096 bytes
                    size = self.fileHandle.readinto(data)
                    #  read next 2048 bytes
                    self.count += 1
                    if self.count > self.junkspersecond:
                        self.SigIncrementCurTime.emit()
                        self.count = 0
                else:
                    time.sleep(0.1)
                    if self.stopix is True:
                        break
            else:
                if self.pausestate is False:
                    time.sleep(1)
                    self.count += 1
                    self.SigIncrementCurTime.emit()
                    size = self.fileHandle.readinto(data)
                else:
                    time.sleep(1)
                    if self.stopix is True:
                        break
        win.fileopened = False  # TODO: manage via signalling, maybe is already

        self.SigFinished.emit()

    def rec_loop(self):
        '''
        Porpose: worker loop for receiving data from STEMLAB server
        data is written to file
        loop runs until EOF or interruption by stopping
        loop can be paused by setting self.pausestate True
        self.pausestate is currently set directly from GUI
        TODO: replace by separate pause methods

        Returns
        -------
        None.

        '''

        self.stopix = False
        data = np.empty(self.DATABLOCKSIZE, dtype=np.float32)
        self.BUFFERFULL = self.DATABLOCKSIZE * 4
        if hasattr(stemlabcontrol, 'data_sock'):
            size = stemlabcontrol.data_sock.recv_into(data)
        else:
            size = 1
            # print("data sock not opened, only test mode")
            
        self.junkspersecond = self.timescaler / (self.JUNKSIZE)
        # print(f"Junkspersec:{self.junkspersecond}")
        self.count = 0
        readbytes = 0
        buf_ix = False
        while size > 0 and self.stopix is False:

            if self.TEST is False:
                if self.pausestate is False:
                    self.mutex.lock()             
                    self.fileHandle.write((data[0:size//4] * 32767).astype(np.int16))
                    # size is the number of bytes received per read operation
                    # from the socket; e.g. DATABLOCKSIZE samples have
                    # DATABLOCKSIZE*8 bytes, the data buffer is specified
                    # for DATABLOCKSIZE float32 elements, i.e. 4 bit words

                    size = stemlabcontrol.data_sock.recv_into(data)
                    if size < self.BUFFERFULL:

                        self.SigBufferUnderflow.emit()
                    #  write next 2048 bytes
                    # TODO: check for replacing clock signalling by other clock
                    readbytes = readbytes + size

                    if readbytes > self.RECSEC:
                        self.SigIncrementCurTime.emit()
                        readbytes = 0
                    self.mutex.unlock()
                else:
                    time.sleep(0.1)
                    if self.stopix is True:
                        break
            else:           # Dummy operations for testing without SDR
                if self.pausestate is False:
                    time.sleep(1)
                    self.count += 1
                    self.SigIncrementCurTime.emit()
                    data[0] = 1
                    self.fileHandle.write((data[0] * 32767).astype(np.int16))
                else:
                    time.sleep(1)
                    if self.stopix is True:
                        break
        win.fileopened = False  # TODO: manage via signalling, maybe is already

        self.SigFinished.emit()

    def stop_loop(self):
        self.stopix = True

    def resetCounter(self):
        self.count = 0


class StemlabControl(QObject):
    '''
    Class for STEMLAB ssh connection, server start and stop,
    data stream socket control and shutdown of the STEMLAB LINUX
    '''

    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

    def set_play(self):
        self.modality = "play"

    def set_rec(self):
        self.modality = "rec"

    def monitor(self):
        # print(f"Stemlabcontrol modality: {self.modality}")
        pass

    def config_socket(self):     ##TODO: atgument (self,modality) 
        '''
        initialize stream socket for communication to sdr_transceiver_wide on
        the STEMLAB
        returns as errorflag 'False' if an error occurs, else it returns 'True'
        In case of unsuccessful socket setup popup error messages are sent
        Returns:
            True if socket can be configures, False in case of error
            requires self.modality to have been set by set_play() or set_rec()
        '''
        self.ctrl_sock = socket(AF_INET, SOCK_STREAM)
        self.ctrl_sock.settimeout(5)
        try:
            self.ctrl_sock.connect((self.HostAddress, 1001))
        except:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Critical)
            msg.setText("Socket Connection Error")
            msg.setInformativeText(
                                  "Cannot establish socket connection "
                                  "for streaming to the STEMLAB")
            msg.setWindowTitle("Socket Connection Error")
            msg.exec_()
            return False

            self.ctrl_sock.settimeout(None)

        self.data_sock = socket(AF_INET, SOCK_STREAM)
        self.data_sock.settimeout(5)
        try:
            self.data_sock.connect((self.HostAddress, 1001))
        except:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Critical)
            msg.setText("Socket Connection Error")
            msg.setInformativeText(
                                  "Cannot establish socket connection "
                                  "for streaming to the STEMLAB")
            msg.setWindowTitle("Socket Connection Error")
            msg.exec_()
            return False

        self.data_sock.settimeout(None)

        if (self.modality != "play") and (self.modality != "rec"):
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Critical)
            msg.setText("Socket Configuration Error")
            msg.setInformativeText("Error , self.modality must be rec or play")
            msg.setWindowTitle("Socket Configuration Error")
            msg.exec_()
            return False

        # send control parameters to ctrl_sock:
        if self.modality == "play":
            self.ctrl_sock.send(pack('<I', 2))
            self.ctrl_sock.send(pack('<I', 0 << 28
                                     | int((1.0 + 1e-6 * win.icorr)
                                           * win.ifreq)))     # TODO: replace win references with local vars and **kwargs from self.sdrparameters
            self.ctrl_sock.send(pack('<I', 1 << 28 | win.rates[win.irate]))
            self.data_sock.send(pack('<I', 3))
        else:
            self.ctrl_sock.send(pack('<I', 0))
            self.ctrl_sock.send(pack('<I', 0 << 28
                                     | int((1.0 + 1e-6 * win.icorr)
                                           * win.ifreq)))
            self.ctrl_sock.send(pack('<I', 1 << 28 | win.rates[win.irate]))

            self.data_sock.send(pack('<I', 1))

        # TODO in further versions: diagnostic output to status message window
        # ("socket started")
        return True

    def startssh(self):
        '''
        login to Host and start ssh session with STEMLAB
        Returns False if a connection error occurs, returns True if
        successful
        '''
        self.HostAddress = win.ui.lineEdit_IPAddress.text()
        port = 22
        username = "root"
        password = "root"
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.ssh.connect(self.HostAddress, port, username, password)
            return True
        except:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Critical)
            msg.setText("Connection Error")
            msg.setInformativeText(
                                  "Cannot connect to Host " + self.HostAddress)
            msg.setWindowTitle("Error")
            msg.exec_()
            return False

    def sshsendcommandseq(self, shcomm):
        '''
        send ssh command string sequence via command string list shcomm
        '''
        count = 0
        while (count < len(shcomm)):  #TODO REM FIN check list, only diagnostic    
            self.ssh.exec_command(shcomm[count])
            count = count + 1
            time.sleep(0.1)

        #TODO: diagnostic output to status message window ("ssh command sent")

    def sdrserverstart(self):
        '''
        Purpose: start server sdr-transceiver-wide on the STEMLAB.
        Stop potentially running server instance before so as to prevent
        undefined communication
        '''

        # TODO: future versions could send diagnostic output to status message indicator
        shcomm = []
        shcomm.append('/bin/bash /sdrstop.sh &')
        shcomm.append('/bin/bash /sdrstart.sh &')

        # connect to remote server via ssh
        if self.startssh() is False:
            return
       # TODO: future versions could send diagnostic output to status message indicator
        self.sdrserverstop()  #TODO ?is this necessary ?
        time.sleep(0.1)
        self.sshsendcommandseq(shcomm)
       # TODO: future versions could send diagnostic output to status message indicator

    def sdrserverstop(self):
        '''
        Purpose: stop server sdr-transceiver-wide on the STEMLAB.
        '''
        shcomm = []
        shcomm.append('/bin/bash /sdrstop.sh &')
        self.sshsendcommandseq(shcomm)

    def RPShutdown(self):
        '''
        Purpose: Shutdown the LINUX running on the STEMLAB
        Sequence:   (1) stop server sdr-transceiver-wide on the STEMLAB.
                    (2) send 'halt' command via ssh, track result via stdout
                    (3) communicate steps and progress via popup messages
        '''

        if self.startssh() is False:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Warning)
            msg.setText("ignoring command")
            msg.setInformativeText(
                              "No Connection to STEMLAB or STEMLAB OS is down")
            msg.setWindowTitle("MISSION IMPOSSIBLE")
            msg.exec_()
            return
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Warning)
        msg.setText("SHUTDOWN")
        msg.setInformativeText(
                              "Shutting down the STEMLAB !"
                              "Please wait until heartbeat stops flashing")
        msg.setWindowTitle("SHUTDOWN")
        msg.exec_()
        self.sdrserverstop()
        stdin, stdout, stderr = self.ssh.exec_command("/sbin/halt >&1 2>&1")
        chout = stdout.channel
        textout = ""
        while True:
            bsout = chout.recv(1)
            textout = textout + bsout.decode("utf-8")
            if not bsout:
                break
        # print(f"stdout: {textout}")
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Information)
        msg.setText("POWER DOWN")
        msg.setInformativeText("It is now safe to power down the STEMLAB")
        msg.setWindowTitle("SHUTDOWN")
        msg.exec_()


class RecorderGUI(QMainWindow):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Constants
        self.TEST = True    # Test Mode Flag for testing the App without a
                            # STEMLAB connected, set True for Test mode
        self.BUFUNDERFLOWTEST = False  # Test for buffer underflow
        self.UTC = False
        self.DATA_FILEEXTENSION = "dat"
        self.CURTIMEINCREMENT = 5
        self.ui = MyRecorder()
        self.ui.setupUi(self)
        # connect button click signals
        self.ui.pushButton_Play.clicked.connect(self.GuiClickPlay)
        self.ui.pushButton_Stop.clicked.connect(self.GuiClickStop)
        self.ui.pushButton_Configuration.clicked.connect(self.editHostAddress)
        self.ui.pushButton_Shutdown.clicked.connect(self.shutdown)
        self.ui.radioButton_LO_bias.clicked.connect(self.activate_LO_bias)
        self.ui.radioButton_Timer.clicked.connect(self.activate_Timer)
        self.ui.pushButton_Pause.clicked.connect(self.GuiClickPause)
        self.ui.pushButton_FastForward.clicked.connect(
                            lambda: self.updatecurtime(self.CURTIMEINCREMENT))
        self.ui.pushButton_Rewind.clicked.connect(
                            lambda: self.updatecurtime(-self.CURTIMEINCREMENT))
        self.ui.lineEditCurTime.returnPressed.connect(
                                                self.GuiEnterCurTime)
        self.ui.lineEdit_IPAddress.returnPressed.connect(self.saveConfig)
        self.ui.pushButton_Rec.clicked.connect(self.GuiClickRec)
        # initialize some GUI elements
        # self.ui.textEdit_Configuration.setFont((QFont('Arial', 12)))  
        self.ui.lineEditCurTime.setFont((QFont('Arial', 18)))
        self.ui.lineEditCurTime.setEnabled(False)
        timestr = str(ndatetime.timedelta(seconds=0))
        self.ui.lineEditCurTime.setText(timestr)
        self.ui.lineEdit_IPAddress.setInputMask('000.000.000.000')
        self.ui.lineEdit_IPAddress.setText("000.000.000.000")
        self.ui.lineEdit_LO_bias.setText("0000")
        self.ui.indicator_Stop.setStyleSheet('background-color: rgb(234,234,234)')
        self.ui.progressBar_time_PLAY.setProperty("value", 0)
        self.ui.closeEvent = self.closeEvent
        self.ui.radioButton_Timer.setEnabled(True)
        self.ui.radioButton_timeract.setEnabled(True)
        # initialize SDR server parameters
        self.rates = {20000:0, 50000:1, 100000:2, 250000:3, 
                      500000:4, 1250000:5, 2500000:6}
        self.configfname = "config_SLsettings.txt"
        self.configParameters = [""]*15     #program is prepared for up to 15
                                            #configuration parameters
                                            #currently only 1 is used
        self.irate = 0            
        self.ifreq = 0
        self.icorr = 0
        self.curtime = 0
        
        # initialize status flags
        self.fileopened = False
        self.pausestate = False
        self.playthreadActive = False
        self.timechanged=False
        self.recording_wait = False
        
        #read config file if it exists
        try:
          with open(self.configfname, 'r') as cfile:
            config_data = cfile.readline()
            self.ui.lineEdit_IPAddress.setText(config_data)
            cfile.close()
            self.IP_address_set = True
        except:
             msg = QMessageBox()
             msg.setIcon(QMessageBox.Warning)
             msg.setText("IP Address Error")
             msg.setInformativeText("STEMLAB IP address not yet set, please define IP address")
             msg.setWindowTitle("Define IP Address")
             msg.exec_()
             self.IP_address_set = False
             self.editHostAddress()
        # start timer tick
        self.timethread = QThread()
        self.timertick = timer_worker()
        self.timertick.moveToThread(self.timethread)
        self.timethread.started.connect(self.timertick.tick)
        self.timertick.SigFinished.connect(self.timethread.quit)
        self.timertick.SigFinished.connect(self.timertick.deleteLater)
        self.timethread.finished.connect(self.timethread.deleteLater)
        self.timertick.SigTick.connect(self.updatetimer)
        self.timethread.start()
        if self.timethread.isRunning():
            self.timethreaddActive = True
        self.metadata = {"center frequency": 1100, "bandwidth": 1250,
                         "starttime": "", "stoptime": "",
                         "location": "XXXXX", "filename": ""}
        self.ismetadata = False
        try:
            stream = open("config_sdr.yaml", "r")
            self.metadata = yaml.safe_load(stream)
            stream.close()
            self.ismetadata = True
        except:
            print("cannot ger metadata")

        # print(c)
        # stream.close()

    def updatetimer(self):
        if self.ui.checkBox_UTC.isChecked():
            self.UTC = True
        else:
            self.UTC = False
        if self.ui.checkBox_TESTMODE.isChecked():
            self.TEST = True
        else:
            self.TEST = False

        if self.UTC:
            dt_now = datetime.now(ndatetime.timezone.utc)
            self.ui.label_showdate.setText(
                dt_now.strftime('%Y-%m-%d'))
            self.ui.label_showtime.setText(
                dt_now.strftime('%H:%M:%S'))
        else:
            dt_now = datetime.now()
            self.ui.label_showdate.setText(
                dt_now.strftime('%Y-%m-%d'))
            self.ui.label_showtime.setText(
                dt_now.strftime('%H:%M:%S'))

        if self.ui.radioButton_timeract.isChecked():
            self.ui.radioButton_Timer.setChecked(False)
            self.ui.dateTimeEdit_setclock.setEnabled(False)
            self.ui.checkBox_UTC.setEnabled(False)
            st = self.ui.dateTimeEdit_setclock.dateTime().toPyDateTime()
            if self.UTC:
                ct = QtCore.QDateTime.currentDateTime().toUTC().toPyDateTime()
            else:
                ct = QtCore.QDateTime.currentDateTime().toPyDateTime()
            self.diff = np.floor((st-ct).total_seconds())
            if self.diff > 0:
                countdown = str(ndatetime.timedelta(seconds=self.diff))
                self.ui.label_ctdn_time.setText(countdown)
            else:
                if self.recording_wait is True:
                    self.recording_wait = False
                    self.recordingsequence()
                return
        else:
            self.ui.checkBox_UTC.setEnabled(True)
            if not self.ui.radioButton_Timer.isChecked():
                self.ui.dateTimeEdit_setclock.setEnabled(False)
            else:
                self.ui.dateTimeEdit_setclock.setEnabled(True)
                
    def activate_Timer(self):


        if self.UTC:
            dt_now = datetime.now(ndatetime.timezone.utc)
            self.ui.label_showdate.setText(
                dt_now.strftime('%Y-%m-%d'))
            self.ui.label_showtime.setText(
                dt_now.strftime('%H:%M:%S'))
        else:
            dt_now = datetime.now()
            self.ui.label_showdate.setText(
                dt_now.strftime('%Y-%m-%d'))
            self.ui.label_showtime.setText(
                dt_now.strftime('%H:%M:%S'))

        if self.ui.radioButton_Timer.isChecked():
            self.ui.checkBox_UTC.setEnabled(False)
            self.ui.dateTimeEdit_setclock.setEnabled(True)
            # ct = QtCore.QDateTime.currentDateTime()
            ct = dt_now
            # TODO: Format to same representation as clock (:,:,:)
            self.ui.dateTimeEdit_setclock.setDateTime(ct)
            self.ui.dateTimeEdit_setclock.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        else:
            self.ui.dateTimeEdit_setclock.setEnabled(False)
            self.ui.checkBox_UTC.setEnabled(True)

    def shutdown(self):
        '''
        Purpose: Callback for SHUTDOWN Button
        Returns: nothing
        '''
        self.GuiClickStop()
        self.timertick.stoptick()
        stemlabcontrol.RPShutdown()

    def editHostAddress(self):     #TODO Check if this is necessary !
        '''
        Purpose: Callback for edidHostAddress Lineedit item
        activate Host IP address field and enable saving mode
        Returns: nothing
        '''
        self.ui.lineEdit_IPAddress.setEnabled(True)
        self.ui.lineEdit_IPAddress.setReadOnly(False)
        self.ui.pushButton_Configuration.clicked.connect(self.saveConfig)
        self.ui.pushButton_Configuration.setText("save IP Address")
        self.ui.pushButton_Configuration.adjustSize()
        self.IP_address_set = False

    def saveConfig(self):
        '''
        Purpose: Callback for IP configuration button
        read entry from Host IP address field and save to configuration file
        Returns: nothing
        '''
        self.ui.lineEdit_IPAddress.setReadOnly(True)
        self.ui.lineEdit_IPAddress.setEnabled(False)
        self.ui.pushButton_Configuration.clicked.connect(self.editHostAddress)
        self.ui.pushButton_Configuration.setText("Set IP Address")
        self.ui.pushButton_Configuration.adjustSize()
        self.configParameters[0] = self.ui.lineEdit_IPAddress.text()
        try:
            with open(self.configfname, 'w') as cfile:
                for x in self.configParameters:
                    cfile.write(x+"\n")
                cfile.close()
                self.IP_address_set = True
        except:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Critical)
            msg.setText("WriteError")
            msg.setInformativeText("Cannot open Config File for writing")
            msg.setWindowTitle("Write Error")
            msg.exec_()

    def filenameparams_extract(self):
        '''
        Purpose: extract control parameters from filename and check for
        consistency with COHIRADIA file name convention
        This could be modified in future versions e.g. when introducing
        to metafiles or fileheaders
        Returns: True, if successful, False otherwise
        '''
        loix = self.my_filename.find('_lo')
        cix = self.my_filename.find('_c')
        rateix = self.my_filename.find('_r')
        errorf = False                            # test for invalid filename
        i_LO_bias = 0
        LObiasraw = self.ui.lineEdit_LO_bias.text()
        LObias_sign = 1

        if LObiasraw[0] == "-":
            LObias_sign = -1
        if LObiasraw.lstrip("-").isnumeric() is True:
            i_LO_bias = LObias_sign*int(LObiasraw.lstrip("-"))
        else:
            errorf = True
            errortxt = "center frequency offset must be integer; Please correct"

        if rateix == -1 or cix == -1 or loix == -1:
            errorf = True
            errortxt = "Probably not a COHIRADIO File \n \
                Filename does not comply with COHIRADIA naming convention"

        freq = self.my_filename[loix+3:rateix]
        self.ui.label_showLO.setText(str(freq))
        freq = freq + '000'
        if freq.isdecimal():
            self.ifreq = int(freq, 10) + 1000*i_LO_bias
        else:
            errorf = True
            errortxt = "Probably not a COHIRADIO File \n \
                Literal after _lo does not comply with COHIRADIA\
                naming convention"

        rate = self.my_filename[rateix+2:cix]
        self.ui.label_showBW.setText(str(rate))
        rate = rate + '000'
        if rate.isdecimal():
            self.irate = int(rate)
        else:
            errorf = True
            errortxt = "Probably not a COHIRADIO File \n \
                Literal after _rate does not comply with COHIRADIA\
                naming convention"

        corr = self.my_filename[cix+2:len(self.my_filename)]

        if corr.isdecimal():
            self.icorr = int(corr)
            if(len(corr) == 0):
                corr = '000'
        else:
            errorf = True
            errortxt = "Probably not a COHIRADIO File \n \
                Literal after _c does not comply with COHIRADIA\
                naming convention"
        if self.ifreq < 0 or self.ifreq > 62500000:
            errorf = True
            errortxt = "center frequency not in range (0 - 62500000) \
                      after _lo\n Probably not a COHIRADIA File"

        if self.irate not in self.rates:
            errorf = True
            errortxt = "sample rate must be in the set: 20000, 50000, 100000,\n \
                      250000, 500000, 1250000, 2500000, \n \
                      Probably not a COHIRADIO File \n \
                      error in string after _r"

        if self.icorr < -100 or self.icorr > 100:
            errorf = True
            errortxt = "frequency correction min ppm must be in \
                      the interval (-100 - 100) after _c \n \
                      Probably not a COHIRADIO File "

        if errorf:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Critical)
            msg.setText("Filename Error")
            msg.setInformativeText(errortxt)
            msg.setWindowTitle("Filename Error")
            msg.exec_()
            return False

        self.sdrparameters = {"ifreq": self.ifreq, "irate": self.irate, "icorr": self.icorr}   ##TODO check if the variables could be kept local

        return True

    def FileOpen(self):
        # TODO: Argument modality: (self,modality)
        '''
        Purpose: 
        If self.modality == "play":
            (1) Open data file for read
            (2) call rotine for extraction of recording parameters from filename
            (3) present recording parameters in info fields
        If self.modality == "rec"
            (1) Create/Open data file for write
            (2) if file already exists and is a valid COHIRADIA data file,
            it is assumed that the old file should be overwritten with the 
            same settings. Thus no new parameter settings are requested
            If file does not yet exist new recording parameters are requested
            via an input dialog, i.e.
                sampling rate (Bandwidth), LO frequency (center)
            (3) present recording parameters in info fields
        Returns: True, if successful, False otherwise
        '''
        if (self.modality != "play") and (self.modality != "rec"):
            # print("Error , self.modality must be rec or play")
            return False

        if self.modality == "play":
            filename =  QtWidgets.QFileDialog.getOpenFileName(self,
                                                              "Open data file"
                                                              , "*.dat", "*."
                                                  + self.DATA_FILEEXTENSION)
            self.f1 = filename[0]  # ## see Recordingbtton
            if not self.f1:
                return False

            self.my_dirname = os.path.dirname(self.f1)
            self.my_filename, ext = os.path.splitext(os.path.basename(self.f1))
            self.ui.lineEdit_Filename.setText(self.my_filename)
            if self.filenameparams_extract() is False:
                # logging.error('filename extraction failed')
                return False
            self.fileHandle = open(self.f1, 'rb')
            self.filesize = os.path.getsize(self.f1)
            self.timescaler = self.irate*4  # scaling factor bytes/s
            self.playlength = math.floor(self.filesize/self.timescaler)
            timestr = time.strftime("%H:%M:%S", time.gmtime(self.playlength))
            self.ui.label_show_playlength.setText(timestr)
        else:
            filename = QtWidgets.QFileDialog.getSaveFileName(self, 
                                                             "Filename for saving:"
                                                             , "*.dat")
            self.metadata['filename'] = filename[0]
            rates = ['2500', '1250', '500', '250', '100', '50', '20']
            rate = "x"
            if self.ismetadata is True:  # check for rate info in metadata
                mrate = self.metadata.get("bandwidth")
                try:
                    rate_index = rates.index(mrate)
                except:
                    rate_index = 1
                try:    
                    mfreq = int(self.metadata.get("center frequency"))
                except:
                    mfreq = 1100
                try:    
                    location = self.metadata.get("location")
                except:
                    location = "XXXXX"
            else:
                rate_index = 1
                mfreq = 1100
                location = "XXXXX"

            while rate.isnumeric() is False:
                # Warning must be integer
                rate, done4 = QtWidgets.QInputDialog.getItem(
                    self, 'Input Dialog', 'Bandwidth', rates, rate_index)

                freq, done2 = QtWidgets.QInputDialog.getInt(
                    self, 'Input Dialog', 'Enter center frequency:', mfreq)

                location, done4 = QtWidgets.QInputDialog.getText(
                    self, 'Log info', 'Enter location:', text=location)
            self.metadata['bandwidth'] = rate
            self.metadata['center frequency'] = str(freq)
            self.metadata['location'] = location
            # stream.close()
            checkstring = "_lo" + str(freq) + "_r" + rate + "_c0."
            islo = filename[0].find("_lo")
            isr = filename[0].find("_r")
            isc0 = filename[0].find("_c0")
            nameraw = filename[0].replace(".dat", "")
            nametrunk = nameraw[0:len(nameraw)]
            # print(f"nameraw: {nameraw}")
            # print(f"nametrunk: {nametrunk}")
            if islo >= 0 and isr >= 0 and isc0 >= 0:
                # the file is most probably an existing COHIRADIA file
                # if so then only the stem of the filename should be reused
                nametrunk = nameraw[0:nameraw.find("_lo")]
                # print("rewrite nametrunk")
            # print(f"nametrunk: {nametrunk}")
            self.f1 = nametrunk + "_lo" + str(freq) + "_r" + rate + "_c0." + self.DATA_FILEEXTENSION
            # print(f"filename: {self.f1}")
            self.my_filename, ext = os.path.splitext(os.path.basename(self.f1))
            self.ui.label_showBW.setText(str(rate))
            self.ui.label_showLO.setText(str(freq))
            self.ifreq = freq*1000
            self.irate = int(rate)*1000
            rectime_ok = False
            while rectime_ok is False:
                self.rectime, done3 = QtWidgets.QInputDialog.getInt(
                    self, 'Input Dialog', 'Enter recording time (min):', 30)
                filesize_forecast = self.irate*4*self.rectime*60/1000000
                # print(f"filesize: {filesize_forecast} MB")
                msg = QMessageBox()
                msg.setWindowTitle("Checkpoint")
                msg.setText(f"Filesize will be {filesize_forecast} MB. "
                             "Do you want to proceed ?")
                msg.setIcon(QMessageBox.Question)
                msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
                msg.buttonClicked.connect(self.popup)
                msg.exec_()

                if self.yesno == "&Yes":
                    rectime_ok = True
                else:
                    rectime_ok = False

            self.timescaler = self.irate*4  # scaling factor bytes/s
            self.fileHandle = open(self.f1, 'wb')
            # self.ui.label_show_playlength.setText("no value")
            self.ui.lineEdit_Filename.setText(self.my_filename)
            timestr = time.strftime("%H:%M:%S", time.gmtime(self.rectime*60))
            self.ui.label_show_playlength.setText(timestr)
            
            #### TODO read/write configs at the end of the recording; https://python.land/data-processing/python-yaml
            stream = open("config_sdr.yaml", "w")
            yaml.dump(self.metadata, stream)
            stream.close()


        self.fileopened = True
        self.curr_time = self.ui.lineEditCurTime.text()
        self.ui.lineEditCurTime.setEnabled(True)
        # logging.info('Successful fileopen' + self.f1)

        return True

    def popup(self,i):
        self.yesno = i.text()
        # print(i.text())

    def playthread_start(self):  # TODO: Argument (self, modality), modality= "recording", "play"
        """_start playback via data stream to STEMLAB sdr server
        starts thread 'playthread' for data streaming to the STEMLAB
        instantiates thread worker method
        initializes signalling_

        :return: _False if error, True on succes_
        :rtype: _Boolean_
        """        

        '''
        Purpose: 
        SigFinished:                 emitted by: thread worker on end of stream
                thread termination, activate callback for Stop button
        thread.finished:
                thread termination
        thread.started:
                start worker method streaming loop
        SigIncrementCurTime:         emitted by: thread worker every second
                increment playtime counter by 1 second
        Returns: False if error, True on success
        '''
        self.playthreadcontrol = {"fileHandle": self.fileHandle,
                                  "timescaler": self.timescaler,
                                  "TEST": self.TEST,
                                  "modality": self.modality}
        self.playthread = QThread()
        self.playrec = playrec_worker(self.playthreadcontrol)
        self.playrec.moveToThread(self.playthread)
        if self.modality == "play":
            self.playthread.started.connect(self.playrec.play_loop)
        else:
            self.playthread.started.connect(self.playrec.rec_loop)

        self.playrec.SigFinished.connect(self.playthread.quit)
        self.playrec.SigFinished.connect(
                                            self.playrec.deleteLater)
        self.playrec.SigFinished.connect(self.GuiClickStop)
        self.playthread.finished.connect(self.playthread.deleteLater)
        self.playrec.SigIncrementCurTime.connect(
                                                lambda: self.updatecurtime(1))
        self.playrec.SigBufferUnderflow.connect(
                                                 lambda: self.bufoverflowsig())
        # TODO: check, if updatecurtime is also controlled from within
        # the recording loop
        self.playthread.start()
        if self.playthread.isRunning():
            self.playthreadActive = True
            # TODO replace playthreadflag by not self.playthread.isFinished()
            return True
        else:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Error)
            msg.setText("internal error")
            msg.setInformativeText("STEMLAB data transfer tread could not be started")
            msg.setWindowTitle("internal error")
            msg.exec_()
            return False

    def GuiClickRec(self):
        """Purpose: Callback for REC button
        calls file create
        starts data streaming from STEMLAB sdr server w method recthread_start

        :return: False on error, True otherwise
        :rtype: Boolean
        """        
        self.ui.checkBox_TESTMODE.setEnabled(False)
        
        if self.IP_address_set is False:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Warning)
            msg.setText("IP Address Error")
            msg.setInformativeText("Enter STEMLAB IP address and press '''save IP Address''' ")
            msg.setWindowTitle("Define IP Address")
            msg.exec_()
            return False

        if self.pausestate is True:
            self.pausestate = False
            self.playrec.pausestate = False
            # self.ui.indicator_Rec.setStyleSheet('background-color: \
            #                                     rgb(255,254,210)') 
            self.ui.indicator_Pause.setStyleSheet('background-color: \
                                                  rgb(234,234,234)')    
            self.ui.label_RECORDING.setStyleSheet(('background-color: \
                                                  rgb(255,50,50)'))
            return True

        stemlabcontrol.set_rec()
        self.modality = "rec"
        self.ui.checkBox_UTC.setEnabled(False)
        if self.playthreadActive is False:
            if self.fileopened is False:
                if self.FileOpen() is False:
                    return False
                self.updatecurtime(0)

            if self.ui.radioButton_timeract.isChecked():
            # TODO: activate self.recordingsequence in timerupdate
                self.recording_wait = True
                return
            else:
                self.recordingsequence()
                self.recording_wait = False

    def recordingsequence(self):
        """start SDR server unless already started. References to 
        :class: `StemlabControl`

        :return: False if STEMLAB socket cannot started
                 False if playback thread cannot be started 
        :rtype: Boolean
        """
        if self.TEST is False:
            stemlabcontrol.sdrserverstart()
            stemlabcontrol.set_rec()
            if stemlabcontrol.config_socket():
                self.playthread_start()
            else:
                return False
        else:
            if self.playthread_start() is False:
                return False

        self.ui.label_RECORDING.setStyleSheet(('background-color: \
                                             rgb(255,50,50)'))
        self.ui.label_RECORDING.setText("RECORDING")
        #####   disable radiobuttons f timer and timerset
        self.ui.radioButton_Timer.setEnabled(False)
        self.ui.radioButton_timeract.setEnabled(False)
        self.ui.indicator_Stop.setStyleSheet('background-color: rgb(234,234,234)')
        self.ui.pushButton_Play.setEnabled(False)

    def GuiClickPlay(self):
        """Callback function for Play Button; Callse file_open
           starts data streaming to STEMLAB sdr server via method playthread_start

        :return: False if IP address not set, File could not be opened
                 False if STEMLAB socket cannot started
                 False if playback thread cannot be started
                 True if self.pausestate is True, self.TEST is True
        :rtype: Boolean
        """        

        #calls file open
        #starts data streaming to STEMLAB sdr server via method playthread_start
        self.ui.checkBox_TESTMODE.setEnabled(False)
        self.ui.checkBox_UTC.setEnabled(False)
        if self.IP_address_set is False:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Warning)
            msg.setText("IP Address Error")
            msg.setInformativeText("Enter STEMLAB IP address ans press '''save IP Address''' ")
            msg.setWindowTitle("Define IP Address")
            msg.exec_()
            return False

        if self.pausestate is True:
            self.pausestate = False
            self.playrec.pausestate = False
            self.ui.label_RECORDING.setStyleSheet(('background-color: \
                                                 rgb(46,210,17)'))
            self.ui.label_RECORDING.setText("PLAY")
            return True
        stemlabcontrol.set_play()
        self.modality = "play"

        if self.playthreadActive is False:
            if self.fileopened is False:
                if self.FileOpen() is False:
                    return False
                self.updatecurtime(0)

            # start server unless already started
            if self.TEST is False:
                stemlabcontrol.sdrserverstart()
                stemlabcontrol.set_play()
                if stemlabcontrol.config_socket():
                    self.playthread_start()

                else:
                    return False
            else:
                if self.playthread_start() is False:
                    return False
            self.ui.label_RECORDING.setStyleSheet(('background-color: \
                                                 rgb(46,210,17)'))
            self.ui.label_RECORDING.setText("PLAY")
            self.ui.indicator_Stop.setStyleSheet('background-color: rgb(234,234,234)')
            self.ui.pushButton_Rec.setEnabled(False)
            return True


    def GuiClickStop(self):
        '''
        Returns
        -------
        None.

        '''
        self.ui.indicator_Play.setStyleSheet('background-color: '
                                             'rgb(234,234,234)')
        self.ui.indicator_Rec.setStyleSheet('background-color: '
                                            'rgb(234,234,234)')
        self.ui.indicator_Stop.setStyleSheet('background-color: '
                                             'rgb(234,234,234)')
        self.ui.pushButton_Rec.setEnabled(True)
        self.ui.pushButton_Play.setEnabled(True)
        self.ui.label_RECORDING.setStyleSheet(('background-color: '
                                               'rgb(229,229,229)'))
        self.ui.label_RECORDING.setText("          ")
        self.ui.checkBox_UTC.setEnabled(True)
        self.ui.checkBox_TESTMODE.setEnabled(True)
        self.ui.radioButton_Timer.setEnabled(True)
        self.ui.radioButton_timeract.setEnabled(True)
        
        if self.playthreadActive is False:
            return
        if self.playthreadActive:
            self.playrec.stop_loop()
            if self.TEST is False:
                stemlabcontrol.sdrserverstop()
            self.updatecurtime(0)        # reset playtime counter
            self.playthreadActive = False

        else:
            self.fileHandle.close()
            self.fileopened = False

    def stop_worker(self):
        if self.playthreadActive:
            self.playrec.stop_loop()
        self.timertick.stoptick()

    def GuiClickPause(self):
        if not self.playthreadActive:
            return
        if self.pausestate is True:
            self.pausestate = False
            self.playrec.pausestate = False
            # self.ui.indicator_Pause.setStyleSheet('background-color: rgb(234,234,234)')
            if self.modality == "play":
                # self.ui.indicator_Play.setStyleSheet('background-color: rgb(163,250,114)')
                self.ui.label_RECORDING.setStyleSheet(('background-color: \
                                                     rgb(46,210,17)'))
                self.ui.label_RECORDING.setText("PLAY")
            if self.modality == "rec":
                # self.ui.indicator_Rec.setStyleSheet('background-color: rgb(234,234,234)')    
                self.ui.label_RECORDING.setStyleSheet(('background-color: \
                                                     rgb(255,50,50)'))
                self.ui.label_RECORDING.setText("RECORDING")
        else:    
            if self.modality == "play":
                #self.ui.indicator_Play.setStyleSheet('background-color: rgb(255,221,51)')
                self.ui.label_RECORDING.setStyleSheet(('background-color: \
                                                     rgb(255,255,127)'))
                self.ui.label_RECORDING.setText("PAUSE PLAY")
            if self.modality == "rec":
                # self.ui.indicator_Rec.setStyleSheet('background-color: rgb(255,221,51)')
                self.ui.label_RECORDING.setStyleSheet(('background-color: \
                                                     rgb(255,170,0)'))
                self.ui.label_RECORDING.setText("PAUSE REC")
            #self.ui.indicator_Pause.setStyleSheet('background-color: rgb(255,254,210)')
            self.pausestate = True
            self.playrec.pausestate = True

    def bufoverflowsig(self):
        if self.BUFUNDERFLOWTEST:
            self.ui.label_RECORDING.setText("UNDERFLOW")


    def activate_LO_bias(self):
        '''
        Returns
        -------
        None.
        Purpose: handle radiobutton for LO bias setting; 
        (1) stop current playback
        (2) conditionally allow for setting new LO bias value
        if button is activated: enable lineEdit to accept new LO bias in kHz
                          else: disable lineEdit and set value to 0
                          

        '''
        self.GuiClickStop()
        if self.ui.radioButton_LO_bias.isChecked() is True:
            self.ui.lineEdit_LO_bias.setEnabled(True)
        else:
            self.ui.lineEdit_LO_bias.setEnabled(False)
            self.ui.lineEdit_LO_bias.setText("0000")
                
    
    def updatecurtime(self,increment):             #increment current time in playtime window and update statusbar
        
        """
        Purpose: increments time indicator by value in increment, except for 0. 
        With increment == 0 the indicator is reset to 0
        if self.modality == "play":
            update progress bar
            set file read pointer to new position
            
        """

        if self.playthreadActive is False:
            return False

        if increment == 0:
            timestr = str(ndatetime.timedelta(seconds=0))
            self.curtime = 0
            win.ui.lineEditCurTime.setText(timestr)
            self.ui.progressBar_time_PLAY.setProperty("value", 0)

        if self.modality == 'rec' and self.pausestate is False:
            if increment != 1:
                return False
            self.curtime += increment
            self.ui.label_RECORDING.setStyleSheet(('background-color: \
                                                    rgb(255,50,50)'))
            self.ui.label_RECORDING.setText("RECORDING")
            progress = self.curtime/(self.rectime*60)
            self.ui.progressBar_time_PLAY.setProperty("value", progress*100)
        if self.modality == 'play' and self.pausestate is False:
            self.size_seconds = self.filesize/self.timescaler

            if self.curtime > 0 and increment < 0:
                self.curtime += increment
            if self.curtime < self.size_seconds and increment > 0:
                self.curtime += increment
            if self.size_seconds > 0:
                progress = self.curtime/self.size_seconds
            else:
                return False
            self.ui.progressBar_time_PLAY.setProperty("value", progress*100)
            position_raw = self.curtime*self.timescaler
            # byte position in file
            position = min(max(0, position_raw-position_raw % 4),
                           self.filesize)
            # guarantee integer multiple of 4, > 0, <= filesize
            if increment != -1 and increment != 1 or self.timechanged == True:
                if self.timechanged == True:
                    self.timechanged = False
                if self.fileopened is True:
                    self.fileHandle.seek(position, 0)
        
        timestr = str(ndatetime.timedelta(seconds=self.curtime))
        win.ui.lineEditCurTime.setText(timestr)
        if self.modality == "rec":
            if self.curtime >= self.rectime*60:
                self.GuiClickStop()
        return True
            
    def GuiEnterCurTime(self):

        timestr = self.ui.lineEditCurTime.text()
        #Convert timestr back to seconds
        diagnostic = timestr.split(":")
        if int(diagnostic[1]) > 59 or int(diagnostic[2]) > 59:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Critical)
            msg.setText("Input Error")
            msg.setInformativeText(
                                  "Minutes and Seconds cannot be > 59")
            msg.setWindowTitle("Please correct")
            msg.exec_()
            return                                                 
        total_seconds = sum(x * int(t) for x, t in zip([3600, 60, 1], timestr.split(":"))) 

        self.curtime = total_seconds
        self.timechanged = True
        
        
if __name__ == '__main__':
    
    #initialize logging method
    #logging.basicConfig(filename='cohiradia.log', encoding='utf-8', level=logging.DEBUG)
    
    if hasattr(QtCore.Qt, 'AA_EnableHighDpiScaling'):
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    if hasattr(QtCore.Qt, 'AA_UseHighDpiPixmaps'):
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)

    
    app = QApplication([])

    win = RecorderGUI()
    app.aboutToQuit.connect(win.stop_worker)    #graceful thread termination on app exit
    win.show()
    stemlabcontrol = StemlabControl()
    sys.exit(app.exec_())

    
        