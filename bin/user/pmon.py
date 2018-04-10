# Copyright 2013 Matthew Wall
"""weewx module that records process information.

Installation

Put this file in the bin/user directory.


Configuration

Add the following to weewx.conf:

[ProcessMonitor]
    data_binding = pmon_binding

[DataBindings]
    [[pmon_binding]]
        database = pmon_sqlite
        manager = weewx.manager.DaySummaryManager
        table_name = archive
        schema = user.pmon.schema

[Databases]
    [[pmon_sqlite]]
        database_name = archive/pmon+.sdb
        database_type = SQLite

[Engine]
    [[Services]]
        archive_services = ..., user.pmon.ProcessMonitor
"""

import os
import platform
import re
import syslog
import time
from subprocess import Popen, PIPE
import resource

import weewx
import weedb
import weeutil.weeutil
from weewx.engine import StdService

VERSION = "0.5.1"

def logmsg(level, msg):
    syslog.syslog(level, 'pmon+: %s' % msg)

def logdbg(msg):
    logmsg(syslog.LOG_DEBUG, msg)

def loginf(msg):
    logmsg(syslog.LOG_INFO, msg)

def logerr(msg):
    logmsg(syslog.LOG_ERR, msg)

#  root@whitebeard:/tmp# sqlite3 /var/lib/weewx/pmon+.sdb
#  SQLite version 3.22.0 2018-01-22 18:45:57
#  Enter ".help" for usage hints.
#  sqlite> ALTER TABLE `archive` ADD `mem_total` INTEGER `swap_used`;
#  sqlite> ALTER TABLE `archive` ADD `mem_free` INTEGER `mem_total`;
#  sqlite> ALTER TABLE `archive` ADD `mem_used` INTEGER `mem_free`;

schema = [
    ('dateTime', 'INTEGER NOT NULL PRIMARY KEY'),
    ('usUnits', 'INTEGER NOT NULL'),
    ('interval', 'INTEGER NOT NULL'),
    ('mem_vsz', 'INTEGER'),
    ('mem_rss', 'INTEGER'),
    ('res_rss', 'INTEGER'),
    ('swap_total', 'INTEGER'),
    ('swap_free', 'INTEGER'),
    ('swap_used', 'INTEGER'),
    ('mem_total', 'INTEGER'),
    ('mem_free', 'INTEGER'),
    ('mem_used', 'INTEGER'),
]

# add the required units and then
# add databinding stanza to [CheetahGenerator] in .conf
weewx.units.obs_group_dict['mem_vsz'] = 'group_data'
weewx.units.obs_group_dict['mem_rss'] = 'group_data'
weewx.units.obs_group_dict['res_rss'] = 'group_data'
weewx.units.obs_group_dict['swap_total'] = 'group_data'
weewx.units.obs_group_dict['swap_free'] = 'group_data'
weewx.units.obs_group_dict['swap_used'] = 'group_data'
weewx.units.obs_group_dict['mem_total'] = 'group_data'
weewx.units.obs_group_dict['mem_free'] = 'group_data'
weewx.units.obs_group_dict['mem_used'] = 'group_data'
weewx.units.USUnits['group_data'] = 'kB'
weewx.units.MetricUnits['group_data'] = 'kB'
#weewx.units.MetricWXUnits['group_data'] = 'kB'
weewx.units.default_unit_format_dict['kB'] = '%.0f'
weewx.units.default_unit_label_dict['kB'] = ' kB'
# 1 Byte = 0.000001 MB (in decimal)
weewx.units.conversionDict['kB'] = {'B': lambda x: x * 0.001}


class ProcessMonitor(StdService):

    def __init__(self, engine, config_dict):
        super(ProcessMonitor, self).__init__(engine, config_dict)

        d = config_dict.get('ProcessMonitor', {})
        self.process = d.get('process', 'weewxd')
        self.max_age = weeutil.weeutil.to_int(d.get('max_age', 2592000))

        # get the database parameters we need to function
        binding = d.get('data_binding', 'pmon_binding')
        self.dbm = self.engine.db_binder.get_manager(data_binding=binding,
                                                     initialize=True)

        # be sure database matches the schema we have
        dbcol = self.dbm.connection.columnsOf(self.dbm.table_name)
        dbm_dict = weewx.manager.get_manager_dict_from_config(config_dict,
                                                              binding)
        memcol = [x[0] for x in dbm_dict['schema']]
        if dbcol != memcol:
            raise Exception('pmon schema mismatch: %s != %s' % (dbcol, memcol))

        self.last_ts = None
        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)

    def shutDown(self):
        try:
            self.dbm.close()
        except weedb.DatabaseError:
            pass

    def new_archive_record(self, event):
        """save data to database then prune old records as needed"""
        now = int(time.time() + 0.5)
        delta = now - event.record['dateTime']
        if delta > event.record['interval'] * 60:
            logdbg("Skipping record: time difference %s too big" % delta)
            return
        if self.last_ts is not None:
            self.save_data(self.get_data(now, self.last_ts))
        self.last_ts = now
        if self.max_age is not None:
            self.prune_data(now - self.max_age)

    def save_data(self, record):
        """save data to database"""
        self.dbm.addRecord(record)

    def prune_data(self, ts):
        """delete records with dateTime older than ts"""
        sql = "delete from %s where dateTime < %d" % (self.dbm.table_name, ts)
        self.dbm.getSql(sql)
        try:
            # sqlite databases need some help to stay small
            self.dbm.getSql('vacuum')
        except weedb.DatabaseError:
            pass

    COLUMNS = re.compile('[\S]+\s+[\d]+\s+[\d.]+\s+[\d.]+\s+([\d]+)\s+([\d]+)')

    def get_data(self, now_ts, last_ts):
        record = dict()
        record['dateTime'] = now_ts
        record['usUnits'] = weewx.METRIC
        record['interval'] = int((now_ts - last_ts) / 60)
        #  get_mem_data()
        try:
            self.wx_pid = str(os.getpid())
            cmd = 'ps up '+self.wx_pid
            loginf("cmd is %s" % cmd)
            p = Popen(cmd, shell=True, stdout=PIPE)
            o = p.communicate()[0]
            for line in o.split('\n'):
                #  loginf("line is %s" % line)
                if line.find(self.process) >= 0:
                    m = self.COLUMNS.search(line)
                    if m:
                        record['mem_vsz'] = int(m.group(1))
                        record['mem_rss'] = int(m.group(2))
        except (ValueError, IOError, KeyError), e:
            logerr('%s failed: %s' % (cmd, e))

        # now get swap data
        # ( from mwalls cmon )
        filename = '/proc/meminfo'
        try:
            mem_ = dict()
            with open(filename) as fp:
                for memline in fp:
                    #  loginf("memline is %s" % memline)
                    if memline.find(':') >= 0:
                        (n, v) = memline.split(':', 1)
                        mem_[n.strip()] = v.strip()
            if mem_:
                # returned values are in kB
                record['mem_total'] = int(mem_['MemTotal'].split()[0])
                record['mem_free'] = int(mem_['MemFree'].split()[0])
                record['mem_used'] = record['mem_total'] - record['mem_free']
                record['swap_total'] = int(mem_['SwapTotal'].split()[0])
                record['swap_free'] = int(mem_['SwapFree'].split()[0])
                record['swap_used'] = record['swap_total'] - record['swap_free']
        except Exception, e:
            logdbg("read failed for %s: %s" % (filename, e))

        record['res_rss'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        return record


# what follows is a basic unit test of this module.  to run the test:
#
# cd /home/weewx
# PYTHONPATH=bin python bin/user/pmon.py
#
if __name__ == "__main__":
    from weewx.engine import StdEngine
    config = {
        'Station': {
            'station_type': 'Simulator',
            'altitude': [0, 'foot'],
            'latitude': 0,
            'longitude': 0},
        'Simulator': {
            'driver': 'weewx.drivers.simulator',
            'mode': 'simulator'},
        'ProcessMonitor': {
            'data_binding': 'pmon_binding',
            'process': 'weewxd'},
        'DataBindings': {
            'pmon_binding': {
                'database': 'pmon_sqlite',
                'manager': 'weewx.manager.DaySummaryManager',
                'table_name': 'archive',
                'schema': 'user.pmon.schema'}},
        'Databases': {
            'pmon_sqlite': {
                'database_name': 'pmon+.sdb',
                'database_type': 'SQLite'}},
        'DatabaseTypes': {
            'SQLite': {
                'driver': 'weedb.sqlite',
                'SQLITE_ROOT': '/var/tmp'}},
        'Engine': {
            'Services': {
                'process_services': 'user.pmon.ProcessMonitor'}}}
    eng = StdEngine(config)
    svc = ProcessMonitor(eng, config)

    nowts = lastts = int(time.time())
    rec = svc.get_data(nowts, lastts)
    print rec

    time.sleep(5)
    nowts = int(time.time())
    rec = svc.get_data(nowts, lastts)
    print rec

    time.sleep(5)
    lastts = nowts
    nowts = int(time.time())
    rec = svc.get_data(nowts, lastts)
    print rec

    os.remove('/var/tmp/pmon+.sdb')
