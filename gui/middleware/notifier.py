#!/usr/bin/env python
#-
# Copyright (c) 2010-2011 iXsystems, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#

""" Helper for FreeNAS to execute command line tools

This helper class abstracts operating system operations like starting,
stopping, restarting services out from the normal Django stuff and makes
future extensions/changes to the command system easier.  When used as a
command line utility, this helper class can also be used to do these
actions.
"""

from collections import defaultdict, OrderedDict
from decimal import Decimal
import ctypes
import errno
import glob
import grp
import libxml2
import logging
import os
import platform
import pwd
import re
import select
import shutil
import signal
import sqlite3
import stat
from subprocess import Popen, PIPE
import subprocess
import sys
import tempfile
import threading
import time
import types

WWW_PATH = "/usr/local/www"
FREENAS_PATH = os.path.join(WWW_PATH, "freenasUI")
NEED_UPDATE_SENTINEL = '/data/need-update'
VERSION_FILE = '/etc/version'
GELI_KEYPATH = '/data/geli'

sys.path.append(WWW_PATH)
sys.path.append(FREENAS_PATH)

os.environ["DJANGO_SETTINGS_MODULE"] = "freenasUI.settings"

from django.db import models
from django.db.models import Q

from freenasUI.common.acl import ACL_FLAGS_OS_WINDOWS, ACL_WINDOWS_FILE
from freenasUI.common.freenasacl import ACL, ACL_Hierarchy
from freenasUI.common.jail import Jls, Jexec
from freenasUI.common.locks import mntlock
from freenasUI.common.pbi import (pbi_add, pbi_delete,
    pbi_info, pbi_create, pbi_makepatch, pbi_patch,
    PBI_ADD_FLAGS_NOCHECKSIG, PBI_ADD_FLAGS_INFO,
    PBI_ADD_FLAGS_EXTRACT_ONLY, PBI_ADD_FLAGS_OUTDIR,
    PBI_ADD_FLAGS_OUTPATH, PBI_ADD_FLAGS_FORCE,
    PBI_INFO_FLAGS_VERBOSE, PBI_CREATE_FLAGS_OUTDIR,
    PBI_CREATE_FLAGS_BACKUP, PBI_MAKEPATCH_FLAGS_OUTDIR,
    PBI_MAKEPATCH_FLAGS_NOCHECKSIG, PBI_PATCH_FLAGS_OUTDIR,
    PBI_PATCH_FLAGS_NOCHECKSIG)
from freenasUI.common.system import get_mounted_filesystems, umount, get_sw_name
from freenasUI.common.warden import (Warden, WardenJail,
    WARDEN_KEY_HOST, WARDEN_KEY_TYPE, WARDEN_KEY_STATUS,
    WARDEN_TYPE_PLUGINJAIL, WARDEN_STATUS_RUNNING)
from freenasUI.middleware import zfs
from freenasUI.middleware.encryption import random_wipe
from freenasUI.middleware.exceptions import MiddlewareError
from freenasUI.middleware.multipath import Multipath

log = logging.getLogger('middleware.notifier')


class Byte(object):
    """
    Used in sysctl hack to return a byte array
    """

    def __init__(self, array, as_string=True):
        self.array = array
        self.as_string = as_string

    @property
    def value(self):
        array = ""
        for byte in self.array:
            if byte == 0 and self.as_string:
                break
            array += chr(byte)
        return array


class StartNotify(threading.Thread):
    """
    Use kqueue to watch for an event before actually calling start/stop
    This should help against synchronization and more responsive notify.
    """

    def __init__(self, pidfile, verb, *args, **kwargs):
        self._pidfile = pidfile
        self._verb = verb
        super(StartNotify, self).__init__(*args, **kwargs)

    def run(self):

        if not self._pidfile:
            return None

        """
        If we are using start or restart we expect that a .pid file will
        exists at the end of the process, so attach to the directory waiting
        for that file.
        Otherwise we will be stopping and expect the .pid to be deleted, so
        attach to the .pid file and wait for it to be removed
        """
        if self._verb in ('start', 'restart'):
            fflags = select.KQ_NOTE_WRITE|select.KQ_NOTE_EXTEND
            _file = os.path.dirname(self._pidfile)
        else:
            fflags = select.KQ_NOTE_WRITE|select.KQ_NOTE_DELETE
            if os.path.exists(self._pidfile):
                _file = self._pidfile
            else:
                _file = os.path.dirname(self._pidfile)
        fd = os.open(_file, os.O_RDONLY)
        evts = [
            select.kevent(fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD|select.KQ_EV_CLEAR,
                fflags=fflags,
            )
        ]
        kq = select.kqueue()
        kq.control(evts, 0, 0)

        tries = 1
        while tries < 4:
            rv = kq.control(None, 2, 1)
            if self._verb in ('start', 'restart'):
                if os.path.exists(self._pidfile):
                    # The file might have been created but it may take a little bit
                    # for the daemon to write the PID
                    time.sleep(0.1)
                if os.path.exists(self._pidfile) and os.stat(self._pidfile).st_size > 0:
                    break
            elif self._verb == "stop" and not os.path.exists(self._pidfile):
                break
            tries += 1
        kq.close()
        os.close(fd)


class notifier:
    from os import system as ___system
    from pwd import getpwnam as ___getpwnam
    IDENTIFIER = 'notifier'
    def __system(self, command):
        log.debug("Executing: %s", command)
        # TODO: python's signal class should be taught about sigprocmask(2)
        # This is hacky hack to work around this issue.
        libc = ctypes.cdll.LoadLibrary("libc.so.7")
        omask = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
        mask = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
        pmask = ctypes.pointer(mask)
        pomask = ctypes.pointer(omask)
        libc.sigprocmask(signal.SIGQUIT, pmask, pomask)
        try:
            self.___system("(" + command + ") 2>&1 | logger -p daemon.notice -t %s"
                           % (self.IDENTIFIER, ))
        finally:
            libc.sigprocmask(signal.SIGQUIT, pomask, None)
        log.debug("Executed: %s", command)

    def __system_nolog(self, command):
        log.debug("Executing: %s", command)
        # TODO: python's signal class should be taught about sigprocmask(2)
        # This is hacky hack to work around this issue.
        libc = ctypes.cdll.LoadLibrary("libc.so.7")
        omask = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
        mask = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
        pmask = ctypes.pointer(mask)
        pomask = ctypes.pointer(omask)
        libc.sigprocmask(signal.SIGQUIT, pmask, pomask)
        try:
            retval = self.___system("(" + command + ") >/dev/null 2>&1")
        finally:
            libc.sigprocmask(signal.SIGQUIT, pomask, None)
        retval >>= 8
        log.debug("Executed: %s; returned %d", command, retval)
        return retval

    def __pipeopen(self, command):
        log.debug("Popen()ing: %s", command)
        return Popen(command, stdin=PIPE, stdout=PIPE, stderr=PIPE, shell=True, close_fds=True)

    def _do_nada(self):
        pass

    def _simplecmd(self, action, what):
        log.debug("Calling: %s(%s) ", action, what)
        f = getattr(self, '_' + action + '_' + what, None)
        if f is None:
            # Provide generic start/stop/restart verbs for rc.d scripts
            if action in ("start", "stop", "restart", "reload"):
                if action == 'restart':
                    self.__system("/usr/sbin/service " + what + " forcestop ")
                self.__system("/usr/sbin/service " + what + " " + action)
                f = self._do_nada
            else:
                raise ValueError("Internal error: Unknown command")
        f()


    __service2daemon = {
            'ssh': ('sshd', '/var/run/sshd.pid'),
            'rsync': ('rsync', '/var/run/rsyncd.pid'),
            'nfs': ('nfsd', None),
            'afp': ('afpd', None),
            'cifs': ('smbd', '/var/run/samba/smbd.pid'),
            'dynamicdns': ('inadyn-mt', None),
            'snmp': ('bsnmpd', '/var/run/snmpd.pid'),
            'ftp': ('proftpd', '/var/run/proftpd.pid'),
            'tftp': ('inetd', '/var/run/inetd.pid'),
            'iscsitarget': ('istgt', '/var/run/istgt.pid'),
            'ups': ('upsd', '/var/db/nut/upsd.pid'),
            'upsmon': ('upsmon', '/var/db/nut/upsmon.pid'),
            'smartd': ('smartd', '/var/run/smartd.pid'),
            'webshell': (None, '/var/run/webshell.pid'),
        }

    def _started_notify(self, verb, what):
        """
        The check for started [or not] processes is currently done in 2 steps
        This is the first step which involves a thread StartNotify that watch for event
        before actually start/stop rc.d scripts

        Returns:
            StartNotify object if the service is known or None otherwise
        """

        if what in self.__service2daemon:
            procname, pidfile = self.__service2daemon[what]
            sn = StartNotify(verb=verb, pidfile=pidfile)
            sn.start()
            return sn
        else:
            return None

    def _started(self, what, notify=None):
        """
        This is the second step::
        Wait for the StartNotify thread to finish and then check for the
        status of pidfile/procname using pgrep

        Returns:
            True whether the service is alive, False otherwise
        """

        if what in self.__service2daemon:
            procname, pidfile = self.__service2daemon[what]
            if notify:
                notify.join()

            if pidfile:
                procname = " " + procname if procname else ""
                retval = self.__pipeopen("/bin/pgrep -F %s%s" % (pidfile, procname)).wait()
            else:
                retval = self.__pipeopen("/bin/pgrep %s" % (procname,)).wait()

            if retval == 0:
                return True
            else:
                return False
        else:
            return False

    def init(self, what, objectid=None, *args, **kwargs):
        """ Dedicated command to create "what" designated by an optional objectid.

        The helper will use method self._init_[what]() to create the object"""
        if objectid is None:
            self._simplecmd("init", what)
        else:
            f = getattr(self, '_init_' + what)
            f(objectid, *args, **kwargs)

    def destroy(self, what, objectid=None):
        if objectid is None:
            raise ValueError("Calling destroy without id")
        else:
            f = getattr(self, '_destroy_' + what)
            f(objectid)

    def start(self, what):
        """ Start the service specified by "what".

        The helper will use method self._start_[what]() to start the service.
        If the method does not exist, it would fallback using service(8)."""
        sn = self._started_notify("start", what)
        self._simplecmd("start", what)
        return self.started(what, sn)

    def started(self, what, sn=None):
        """ Test if service specified by "what" has been started. """
        f = getattr(self, '_started_' + what, None)
        if callable(f):
            return f()
        else:
            return self._started(what, sn)

    def stop(self, what):
        """ Stop the service specified by "what".

        The helper will use method self._stop_[what]() to stop the service.
        If the method does not exist, it would fallback using service(8)."""
        sn = self._started_notify("stop", what)
        self._simplecmd("stop", what)
        return self.started(what, sn)

    def restart(self, what):
        """ Restart the service specified by "what".

        The helper will use method self._restart_[what]() to restart the service.
        If the method does not exist, it would fallback using service(8)."""
        sn = self._started_notify("restart", what)
        self._simplecmd("restart", what)
        return self.started(what, sn)

    def reload(self, what):
        """ Reload the service specified by "what".

        The helper will use method self._reload_[what]() to reload the service.
        If the method does not exist, the helper will try self.restart of the
        service instead."""
        try:
            self._simplecmd("reload", what)
        except:
            self.restart(what)
        return self.started(what)

    def change(self, what):
        """ Notify the service specified by "what" about a change.

        The helper will use method self.reload(what) to reload the service.
        If the method does not exist, the helper will try self.start the
        service instead."""
        try:
            self.reload(what)
        except:
            self.start(what)

    def _start_webshell(self):
        self.__system_nolog("/usr/local/bin/python /usr/local/www/freenasUI/tools/webshell.py")

    def _restart_webshell(self):
        try:
            with open('/var/run/webshell.pid', 'r') as f:
                pid = f.read()
                os.kill(int(pid), signal.SIGHUP)
                time.sleep(0.2)
        except:
            pass
        self.__system_nolog("/usr/local/bin/python /usr/local/www/freenasUI/tools/webshell.py")

    def _restart_iscsitarget(self):
        self.__system("/usr/sbin/service ix-istgt quietstart")
        self.__system("/usr/sbin/service istgt forcestop")
        self.__system("/usr/sbin/service istgt restart")

    def _restart_collectd(self):
        self.__system("/usr/sbin/service ix-collectd quietstart")
        self.__system("/usr/sbin/service collectd restart")

    def _start_iscsitarget(self):
        self.__system("/usr/sbin/service ix-istgt quietstart")
        self.__system("/usr/sbin/service istgt restart")

    def _stop_iscsitarget(self):
        self.__system("/usr/sbin/service istgt forcestop")

    def _reload_iscsitarget(self):
        self.__system("/usr/sbin/service ix-istgt quietstart")
        self.__system("/usr/sbin/service istgt reload")

    def _start_sysctl(self):
        self.__system("/usr/sbin/service sysctl start")
        self.__system("/usr/sbin/service ix-sysctl quietstart")

    def _reload_sysctl(self):
        self.__system("/usr/sbin/service sysctl start")
        self.__system("/usr/sbin/service ix-sysctl reload")

    def _start_network(self):
        c = self.__open_db()
        c.execute("SELECT COUNT(n.id) FROM network_interfaces n LEFT JOIN network_alias a ON a.alias_interface_id=n.id WHERE int_ipv6auto = 1 OR int_ipv6address != '' OR alias_v6address != ''")
        ipv6_interfaces = c.fetchone()[0]
        if ipv6_interfaces > 0:
            try:
                auto_linklocal = self.sysctl("net.inet6.ip6.auto_linklocal", _type='INT')
            except AssertionError:
                auto_linklocal = 0
            if auto_linklocal == 0:
                self.__system("/sbin/sysctl net.inet6.ip6.auto_linklocal=1")
                self.__system("/usr/sbin/service autolink auto_linklocal quietstart")
                self.__system("/usr/sbin/service netif stop")
        self.__system("/etc/netstart")

    def ifconfig_alias(self, iface, oldip=None, newip=None, oldnetmask=None, newnetmask=None):
        if not iface:
            return False

        cmd = "/sbin/ifconfig %s" % iface
        if newip and newnetmask:
            cmd += " alias %s/%s" % (newip, newnetmask)

        elif newip:
            cmd += " alias %s" % newip

        else:
            cmd = None

        if cmd:
            p = self.__pipeopen(cmd)
            if p.wait() != 0:
                return False

        cmd = "/sbin/ifconfig %s" % iface
        if newip:
            cmd += " -alias %s" % oldip
            p = self.__pipeopen(cmd)
            if p.wait() != 0:
                return False

        if newnetmask and not newip:
            cmd += " alias %s/%s" % (oldip, newnetmask)

        else:
            cmd = None

        if cmd:
            p = self.__pipeopen(cmd)
            if p.wait() != 0:
                return False

        return True

    def _reload_named(self):
        self.__system("/usr/sbin/service named reload")

    def _reload_networkgeneral(self):
        self.__system('/bin/hostname ""')
        self.__system("/usr/sbin/service ix-hostname quietstart")
        self.__system("/usr/sbin/service hostname quietstart")
        self.__system("/usr/sbin/service routing restart")

    def _reload_timeservices(self):
        self.__system("/usr/sbin/service ix-localtime quietstart")
        self.__system("/usr/sbin/service ix-ntpd quietstart")
        self.__system("/usr/sbin/service ntpd restart")
        c = self.__open_db()
        c.execute("SELECT stg_timezone FROM system_settings ORDER BY -id LIMIT 1")
        os.environ['TZ'] = c.fetchone()[0]
        time.tzset()

    def _restart_smartd(self):
        self.__system("/usr/sbin/service ix-smartd quietstart")
        self.__system("/usr/sbin/service smartd forcestop")
        self.__system("/usr/sbin/service smartd restart")

    def _reload_ssh(self):
        self.__system("/usr/sbin/service ix-sshd quietstart")
        self.__system("/usr/sbin/service sshd reload")

    def _start_ssh(self):
        self.__system("/usr/sbin/service ix-sshd quietstart")
        self.__system("/usr/sbin/service sshd start")

    def _stop_ssh(self):
        self.__system("/usr/sbin/service sshd forcestop")

    def _restart_ssh(self):
        self.__system("/usr/sbin/service ix-sshd quietstart")
        self.__system("/usr/sbin/service sshd forcestop")
        self.__system("/usr/sbin/service sshd restart")

    def _reload_rsync(self):
        self.__system("/usr/sbin/service ix-rsyncd quietstart")
        self.__system("/usr/sbin/service rsyncd restart")

    def _restart_rsync(self):
        self.__system("/usr/sbin/service ix-rsyncd quietstart")
        self.__system("/usr/sbin/service rsyncd forcestop")
        self.__system("/usr/sbin/service rsyncd restart")

    def _get_stg_directoryservice(self):
        c = self.__open_db()
        c.execute("SELECT stg_directoryservice FROM system_settings ORDER BY -id LIMIT 1")
        return c.fetchone()[0] 

    def _started_nis(self):
        res = False 
        if self._get_stg_directoryservice() == 'nis':
            res = self.__system_nolog("/etc/directoryservice/NIS/ctl status")
        return (True if res == 0 else False)

    def _start_nis(self):
        res = False 
        if self._get_stg_directoryservice() == 'nis':
            res = self.__system_nolog("/etc/directoryservice/NIS/ctl start")
        return (True if res == 0 else False)

    def _restart_nis(self):
        res = False 
        if self._get_stg_directoryservice() == 'nis':
            res = self.__system_nolog("/etc/directoryservice/NIS/ctl restart")
        return (True if res == 0 else False)

    def _stop_nis(self):
        res = False 
        if self._get_stg_directoryservice() == 'nis':
            res = self.__system_nolog("/etc/directoryservice/NIS/ctl stop")
        return (True if res == 0 else False)

    def _started_ldap(self):
        from freenasUI.common.freenasldap import FreeNAS_LDAP, FLAGS_DBINIT
        from freenasUI.common.system import ldap_enabled

        ret = False
        if ldap_enabled():
            f = FreeNAS_LDAP(flags=FLAGS_DBINIT)
            f.open()
            if f.isOpen():
                ret = True
            else:
                ret = False
            f.close()

        return ret

    def _start_ldap(self):
        res = False 
        if self._get_stg_directoryservice() == 'ldap':
            res = self.__system_nolog("/etc/directoryservice/LDAP/ctl start")
        return (True if res == 0 else False)

    def _stop_ldap(self):
        res = False 
        if self._get_stg_directoryservice() == 'ldap':
            res = self.__system_nolog("/etc/directoryservice/LDAP/ctl stop")
        return (True if res == 0 else False)

    def _restart_ldap(self):
        res = False 
        if self._get_stg_directoryservice() == 'ldap':
            res = self.__system_nolog("/etc/directoryservice/LDAP/ctl restart")
        return (True if res == 0 else False)

    def _clear_activedirectory_config(self):
        self.__system("/bin/rm -f /etc/directoryservice/ActiveDirectory/config")

    def _started_nt4(self):
        res = False 
        if self._get_stg_directoryservice() == 'nt4':
            res = self.__system_nolog("/etc/rc.d/ix-nt4 status")
        return (True if res == 0 else False)

    def _start_nt4(self):
        res = False 
        if self._get_stg_directoryservice() == 'nt4':
            res = self.__system_nolog("/etc/directoryservice/NT4/ctl start")
        return (True if res == 0 else False)

    def _restart_nt4(self):
        res = False 
        if self._get_stg_directoryservice() == 'nt4':
            res = self.__system_nolog("/etc/directoryservice/NT4/ctl restart")
        return (True if res == 0 else False)

    def _stop_nt4(self):
        res = False 
        if self._get_stg_directoryservice() == 'nt4':
            res = self.__system_nolog("/etc/directoryservice/NT4/ctl stop")
        return (True if res == 0 else False)

    def _started_activedirectory(self):
        from freenasUI.common.freenasldap import (FreeNAS_ActiveDirectory, FLAGS_DBINIT)
        from freenasUI.common.system import activedirectory_enabled

        for srv in ('kinit', 'activedirectory', ):
            if (self.__system_nolog('/usr/sbin/service ix-%s status' % (srv, ))
                != 0):
                return False

        ret = False
        if activedirectory_enabled():
            f = FreeNAS_ActiveDirectory(flags=FLAGS_DBINIT)
            f.open()
            if f.isOpen():
                ret = True
            else:
                ret = False
            f.close()

        return ret

    def _start_activedirectory(self):
        res = False
        if self._get_stg_directoryservice() == 'activedirectory':
            res = self.__system_nolog("/etc/directoryservice/ActiveDirectory/ctl start")
        return (True if res == 0 else False)

    def _stop_activedirectory(self):
        res = False
        if self._get_stg_directoryservice() == 'activedirectory':
            res = self.__system_nolog("/etc/directoryservice/ActiveDirectory/ctl stop")
        return (True if res == 0 else False)

    def _restart_activedirectory(self):
        res = False
        if self._get_stg_directoryservice() == 'activedirectory':
            res = self.__system_nolog("/etc/directoryservice/ActiveDirectory/ctl restart")
        return (True if res == 0 else False)

    def _restart_syslogd(self):
        self.__system("/usr/sbin/service ix-syslogd quietstart")
        self.__system("/usr/sbin/service syslogd restart")

    def _start_syslogd(self):
        self.__system("/usr/sbin/service ix-syslogd quietstart")
        self.__system("/usr/sbin/service syslogd start")

    def _reload_tftp(self):
        self.__system("/usr/sbin/service ix-inetd quietstart")
        self.__system("/usr/sbin/service inetd forcestop")
        self.__system("/usr/sbin/service inetd restart")

    def _restart_tftp(self):
        self.__system("/usr/sbin/service ix-inetd quietstart")
        self.__system("/usr/sbin/service inetd forcestop")
        self.__system("/usr/sbin/service inetd restart")

    def _restart_cron(self):
        self.__system("/usr/sbin/service ix-crontab quietstart")
        self.__system("/usr/sbin/service cron restart")

    def _start_motd(self):
        self.__system("/usr/sbin/service ix-motd quietstart")
        self.__system("/usr/sbin/service motd quietstart")

    def _start_ttys(self):
        self.__system("/usr/sbin/service ix-ttys quietstart")

    def _reload_ftp(self):
        self.__system("/usr/sbin/service ix-proftpd quietstart")
        self.__system("/usr/sbin/service proftpd restart")

    def _restart_ftp(self):
        self.__system("/usr/sbin/service ix-proftpd quietstart")
        self.__system("/usr/sbin/service proftpd forcestop")
        self.__system("/usr/sbin/service proftpd restart")
        self.__system("sleep 1")

    def _start_ftp(self):
        self.__system("/usr/sbin/service ix-proftpd quietstart")
        self.__system("/usr/sbin/service proftpd start")

    def _start_ups(self):
        self.__system("/usr/sbin/service ix-ups quietstart")
        self.__system("/usr/sbin/service nut start")
        self.__system("/usr/sbin/service nut_upsmon start")
        self.__system("/usr/sbin/service nut_upslog start")

    def _stop_ups(self):
        self.__system("/usr/sbin/service nut_upslog forcestop")
        self.__system("/usr/sbin/service nut_upsmon forcestop")
        self.__system("/usr/sbin/service nut forcestop")

    def _restart_ups(self):
        self.__system("/usr/sbin/service ix-ups quietstart")
        self.__system("/usr/sbin/service nut forcestop")
        self.__system("/usr/sbin/service nut_upsmon forcestop")
        self.__system("/usr/sbin/service nut_upslog forcestop")
        self.__system("/usr/sbin/service nut restart")
        self.__system("/usr/sbin/service nut_upsmon restart")
        self.__system("/usr/sbin/service nut_upslog restart")

    def _started_ups(self):
        from freenasUI.services.models import UPS
        mode = UPS.objects.order_by('-id')[0].ups_mode
        if mode == "master":
            svc = "ups"
        else:
            svc = "upsmon"
        sn = self._started_notify("start", "upsmon")
        return self._started(svc, sn)

    def _load_afp(self):
        self.__system("/usr/sbin/service ix-afpd quietstart")
        self.__system("/usr/sbin/service dbus quietstart")
        self.__system("/usr/sbin/service avahi-daemon quietstart")
        self.__system("/usr/sbin/service netatalk quietstart")

    def _start_afp(self):
        self.__system("/usr/sbin/service ix-afpd start")
        self.__system("/usr/sbin/service dbus start")
        self.__system("/usr/sbin/service avahi-daemon start")
        self.__system("/usr/sbin/service netatalk start")

    def _stop_afp(self):
        self.__system("/usr/sbin/service netatalk forcestop")
        #FIXME: fix rc.d/netatalk to honor the force verbs properly.
        self.__system("killall afpd")
        self.__system("killall cnid_metad")
        self.__system("killall cnid_dbd")
        self.__system("/usr/sbin/service avahi-daemon forcestop")
        self.__system("/usr/sbin/service dbus forcestop")

    def _restart_afp(self):
        self._stop_afp()
        self._start_afp()

    def _reload_afp(self):
        self.__system("/usr/sbin/service ix-afpd quietstart")
        self.__system("killall -1 avahi-daemon")
        self.__system("killall -1 afpd")

    def _reload_nfs(self):
        self.__system("/usr/sbin/service ix-nfsd quietstart")
        self.__system("/usr/sbin/service mountd reload")

    def _restart_nfs(self):
        self.__system("/usr/sbin/service lockd forcestop")
        self.__system("/usr/sbin/service statd forcestop")
        self.__system("/usr/sbin/service mountd forcestop")
        self.__system("/usr/sbin/service nfsd forcestop")
        self.__system("/usr/sbin/service ix-nfsd quietstart")
        self.__system("/usr/sbin/service mountd quietstart")
        self.__system("/usr/sbin/service nfsd quietstart")
        self.__system("/usr/sbin/service statd quietstart")
        self.__system("/usr/sbin/service lockd quietstart")

    def _stop_nfs(self):
        self.__system("/usr/sbin/service lockd forcestop")
        self.__system("/usr/sbin/service statd forcestop")
        self.__system("/usr/sbin/service mountd forcestop")
        self.__system("/usr/sbin/service nfsd forcestop")

    def _start_nfs(self):
        self.__system("/usr/sbin/service ix-nfsd quietstart")
        self.__system("/usr/sbin/service mountd quietstart")
        self.__system("/usr/sbin/service nfsd quietstart")
        self.__system("/usr/sbin/service statd quietstart")
        self.__system("/usr/sbin/service lockd quietstart")

    def _force_stop_jail(self):
        self.__system("/usr/sbin/service jail forcestop")

    def _start_plugins(self, jail=None, plugin=None):
        if jail and plugin:
            self.__system_nolog("/usr/sbin/service ix-plugins forcestart %s:%s" % (jail, plugin))
        else:
            self.__system_nolog("/usr/sbin/service ix-plugins forcestart")

    def _stop_plugins(self, jail=None, plugin=None):
        if jail and plugin:
            self.__system_nolog("/usr/sbin/service ix-plugins forcestop %s:%s" % (jail, plugin))
        else:
            self.__system_nolog("/usr/sbin/service ix-plugins forcestop")

    def _restart_plugins(self, jail=None, plugin=None):
        self._stop_plugins(jail, plugin)
        self._start_plugins(jail, plugin)

    def _started_plugins(self, jail=None, plugin=None):
        res = False
        if jail and plugin:
            if self.__system_nolog("/usr/sbin/service ix-plugins status %s:%s" % (jail, plugin)) == 0:
                res = True 
        else: 
            if self.__system_nolog("/usr/sbin/service ix-plugins status") == 0:
                res = True 
        return res

    def pluginjail_running(self, pjail=None):
        running = False

        try:
            wlist = Warden().list()
            for wj in wlist:
                wj = WardenJail(**wj)
                if pjail and wj.host == pjail:
                    if wj.type == WARDEN_TYPE_PLUGINJAIL and \
                        wj.status == WARDEN_STATUS_RUNNING:
                        running = True
                        break

                elif not pjail and wj.type == WARDEN_TYPE_PLUGINJAIL and \
                    wj.status == WARDEN_STATUS_RUNNING:
                    running = True
                    break
        except:
            pass

        return running

    def start_ataidle(self, what=None):
        if what is not None:
            self.__system("/usr/sbin/service ix-ataidle quietstart %s" % what)
        else:
            self.__system("/usr/sbin/service ix-ataidle quietstart")

    def start_ssl(self, what=None):
        if what is not None:
            self.__system("/usr/sbin/service ix-ssl quietstart %s" % what)
        else:
            self.__system("/usr/sbin/service ix-ssl quietstart")

    def _restart_dynamicdns(self):
        self.__system("/usr/sbin/service ix-inadyn quietstart")
        self.__system("/usr/sbin/service inadyn-mt forcestop")
        self.__system("/usr/sbin/service inadyn-mt restart")

    def _restart_system(self):
        self.__system("/bin/sleep 3 && /sbin/shutdown -r now &")

    def _stop_system(self):
        self.__system("/sbin/shutdown -p now")

    def _reload_cifs(self):
        self.__system("/usr/sbin/service ix-samba quietstart")
        self.__system("killall -1 avahi-daemon")
        self.__system("/usr/sbin/service samba forcereload")

    def _restart_cifs(self):
        self.__system("/usr/sbin/service ix-samba quietstart")
        self.__system("/usr/sbin/service dbus forcestop")
        self.__system("/usr/sbin/service dbus restart")
        self.__system("/usr/sbin/service avahi-daemon forcestop")
        self.__system("/usr/sbin/service avahi-daemon restart")
        self.__system("/usr/sbin/service samba forcestop")
        self.__system("/usr/sbin/service samba quietrestart")

    def _start_cifs(self):
        self.__system("/usr/sbin/service ix-samba quietstart")
        self.__system("/usr/sbin/service dbus quietstart")
        self.__system("/usr/sbin/service avahi-daemon quietstart")
        self.__system("/usr/sbin/service samba quietstart")

    def _stop_cifs(self):
        self.__system("/usr/sbin/service dbus forcestop")
        self.__system("/usr/sbin/service dbus restart")
        self.__system("/usr/sbin/service avahi-daemon forcestop")
        self.__system("/usr/sbin/service avahi-daemon restart")
        self.__system("/usr/sbin/service samba forcestop")

    def _start_snmp(self):
        self.__system("/usr/sbin/service ix-bsnmpd quietstart")
        self.__system("/usr/sbin/service bsnmpd quietstart")

    def _stop_snmp(self):
        self.__system("/usr/sbin/service bsnmpd quietstop")

    def _restart_snmp(self):
        self.__system("/usr/sbin/service ix-bsnmpd quietstart")
        self.__system("/usr/sbin/service bsnmpd forcestop")
        self.__system("/usr/sbin/service bsnmpd quietstart")

    def _restart_http(self):
        self.__system("/usr/sbin/service ix-nginx quietstart")
        self.__system("/usr/sbin/service nginx restart")

    def _reload_http(self):
        self.__system("/usr/sbin/service ix-nginx quietstart")
        self.__system("/usr/sbin/service nginx reload")

    def _reload_loader(self):
        self.__system("/usr/sbin/service ix-loader reload")

    def _start_loader(self):
        self.__system("/usr/sbin/service ix-loader quietstart")

    def __saver_loaded(self):
        pipe = os.popen("kldstat|grep daemon_saver")
        out = pipe.read().strip('\n')
        pipe.close()
        return (len(out) > 0)

    def _start_saver(self):
        if not self.__saver_loaded():
            self.__system("kldload daemon_saver")

    def _stop_saver(self):
        if self.__saver_loaded():
            self.__system("kldunload daemon_saver")

    def _restart_saver(self):
        self._stop_saver()
        self._start_saver()

    def __open_db(self, ret_conn=False):
        """Open and return a cursor object for database access."""
        try:
            from freenasUI.settings import DATABASES
            dbname = DATABASES['default']['NAME']
        except:
            dbname = '/data/freenas-v1.db'

        conn = sqlite3.connect(dbname)
        c = conn.cursor()
        if ret_conn:
            return c, conn
        return c

    def __gpt_labeldisk(self, type, devname, swapsize=2):
        """Label the whole disk with GPT under the desired label and type"""

        # Calculate swap size.
        swapgb = swapsize
        swapsize = swapsize * 1024 * 1024 * 2
        # Round up to nearest whole integral multiple of 128 and subtract by 34
        # so next partition starts at mutiple of 128.
        swapsize = ((swapsize+127)/128)*128
        # To be safe, wipe out the disk, both ends... before we start
        self.__system("dd if=/dev/zero of=/dev/%s bs=1m count=1" % (devname, ))
        try:
            p1 = self.__pipeopen("diskinfo %s" % (devname, ))
            size = int(re.sub(r'\s+', ' ', p1.communicate()[0]).split()[2]) / (1024)
        except:
            log.error("Unable to determine size of %s", devname)
        else:
            # The GPT header takes about 34KB + alignment, round it to 100
            if size - 100 <= swapgb * 1024 * 1024:
                raise MiddlewareError('Your disk size must be higher than %dGB' % (swapgb, ))
            # HACK: force the wipe at the end of the disk to always succeed. This
            # is a lame workaround.
            self.__system("dd if=/dev/zero of=/dev/%s bs=1m oseek=%s" % (
                devname,
                size / 1024 - 4,
                ))

        commands = []
        commands.append("gpart create -s gpt /dev/%s" % (devname, ))
        if swapsize > 0:
            commands.append("gpart add -b 128 -t freebsd-swap -s %d %s" % (swapsize, devname))
            commands.append("gpart add -t %s %s" % (type, devname))
        else:
            commands.append("gpart add -b 128 -t %s %s" % (type, devname))

        # Install a dummy boot block so system gives meaningful message if booting
        # from the wrong disk.
        commands.append("gpart bootcode -b /boot/pmbr-datadisk /dev/%s" % (devname))

        for command in commands:
            proc = self.__pipeopen(command)
            proc.wait()
            if proc.returncode != 0:
                raise MiddlewareError('Unable to GPT format the disk "%s"' % devname)

        # We might need to sync with reality (e.g. devname -> uuid)
        # Invalidating confxml is required or changes wont be seen
        self.__confxml = None
        self.sync_disk(devname)

    def __gpt_unlabeldisk(self, devname):
        """Unlabel the disk"""
        swapdev = self.part_type_from_device('swap', devname)
        if swapdev != '':
            self.__system("swapoff /dev/%s.eli" % swapdev)
            self.__system("geli detach /dev/%s" % swapdev)
        self.__system("gpart destroy -F /dev/%s" % devname)

        # Wipe out the partition table by doing an additional iterate of create/destroy
        self.__system("gpart create -s gpt /dev/%s" % devname)
        self.__system("gpart destroy -F /dev/%s" % devname)

        # We might need to sync with reality (e.g. uuid -> devname)
        # Invalidating confxml is required or changes wont be seen
        self.__confxml = None
        self.sync_disk(devname)

    def unlabel_disk(self, devname):
        # TODO: Check for existing GPT or MBR, swap, before blindly call __gpt_unlabeldisk
        self.__gpt_unlabeldisk(devname)

    def __encrypt_device(self, devname, diskname, volume, passphrase=None):
        from freenasUI.storage.models import Disk, EncryptedDisk

        geli_keyfile = volume.get_geli_keyfile()
        if not os.path.exists(geli_keyfile):
            if not os.path.exists(GELI_KEYPATH):
                self.__system("mkdir -p %s" % (GELI_KEYPATH, ))
            self.__system("dd if=/dev/random of=%s bs=64 count=1" % (geli_keyfile, ))

        if passphrase is not None:
            _passphrase = " -J %s" % passphrase
            _passphrase2 = "-j %s" % passphrase
        else:
            _passphrase = "-P"
            _passphrase2 = "-p"

        self.__system("geli init -s 4096 -B none %s -K %s /dev/%s" % (_passphrase, geli_keyfile, devname))
        self.__system("geli attach %s -k %s /dev/%s" % (_passphrase2, geli_keyfile, devname))
        # TODO: initialize the provider in background (wipe with random data)

        ident = self.device_to_identifier(diskname)
        diskobj = Disk.objects.get(disk_identifier=ident)
        encdiskobj = EncryptedDisk()
        encdiskobj.encrypted_volume = volume
        encdiskobj.encrypted_disk = diskobj
        encdiskobj.encrypted_provider = devname
        encdiskobj.save()

        return ("/dev/%s.eli" % devname)

    def geli_setkey(self, dev, key, passphrase=None, slot=0):
        command = ["geli", "setkey", "-n", str(slot)]
        if passphrase:
            command.extend(["-J", passphrase])
        else:
            command.append("-P")
        command.extend(["-K", key, dev])
        proc = self.__pipeopen(' '.join(command))
        err = proc.communicate()[1]
        if proc.returncode != 0:
            raise MiddlewareError("Unable to set passphrase: %s" % (err, ))

    def geli_passphrase(self, volume, passphrase, rmrecovery=False):
        """
        Set a passphrase in a geli
        If passphrase is None then remove the passphrase

        Raises:
            MiddlewareError
        """
        geli_keyfile = volume.get_geli_keyfile()
        if passphrase:
            _passphrase = "-J %s" % (passphrase, )
        else:
            _passphrase = "-P"
        for ed in volume.encrypteddisk_set.all():
            dev = ed.encrypted_provider
            """
            A new passphrase cannot be set without destroying the recovery key
            """
            if rmrecovery is True:
                proc = self.__pipeopen("geli delkey -n 1 %s" % (dev, ))
                proc.communicate()
            proc = self.__pipeopen("geli setkey -n 0 %s -K %s %s" % (
                _passphrase,
                geli_keyfile,
                dev,
                )
            )
            err = proc.communicate()[1]
            if proc.returncode != 0:
                raise MiddlewareError("Unable to set passphrase: %s" % (err, ))

    def geli_rekey(self, volume, passphrase=None, slot=0):
        """
        Regenerates the geli global key and set it to devs

        Raises:
            MiddlewareError
        """

        geli_keyfile = volume.get_geli_keyfile()

        # Generate new key as .tmp
        self.__system("dd if=/dev/random of=%s.tmp bs=64 count=1" % (geli_keyfile, ))
        error = False
        applied = []
        if passphrase:
            _passphrase = "-J %s" % (passphrase, )
        else:
            _passphrase = "-P"
        for ed in volume.encrypteddisk_set.all():
            dev = ed.encrypted_provider
            proc = self.__pipeopen("geli setkey %s -n %d -K %s.tmp %s" % (
                _passphrase,
                slot,
                geli_keyfile,
                dev,
                )
            )
            err = proc.communicate()[1]
            if proc.returncode != 0:
                error = True
                break
            applied.append(dev)

        # Try to be atomic in a certain way
        # If rekey failed for one of the devs, revert for the ones already applied
        if error:
            for dev in applied:
                proc = self.__pipeopen("geli setkey %s -n %d -k %s.tmp -K %s %s" % (
                    _passphrase,
                    slot,
                    geli_keyfile,
                    geli_keyfile,
                    dev,
                    )
                )
                proc.communicate()
            raise MiddlewareError("Unable to set key: %s" % (err, ))
        else:
            self.__system("mv %s.tmp %s" % (geli_keyfile, geli_keyfile))

    def geli_recoverykey_add(self, volume, passphrase=None):

        reckey_file = tempfile.mktemp(dir='/tmp/')
        self.__system("dd if=/dev/random of=%s bs=64 count=1" % (reckey_file, ))

        for ed in volume.encrypteddisk_set.all():
            dev = ed.encrypted_provider
            if passphrase is not None:
                proc = self.__pipeopen("geli setkey -n 1 -K %s -J %s %s" % (
                    reckey_file,
                    passphrase,
                    dev,
                    )
                    )
            else:
                proc = self.__pipeopen("geli setkey -n 1 -K %s -P %s" % (
                    reckey_file,
                    dev,
                    )
                    )
            err = proc.communicate()[1]
            if proc.returncode != 0:
                raise MiddlewareError("Unable to set recovery key: %s" % (err, ))
        return reckey_file

    def geli_delkey(self, volume, slot=1):

        for ed in volume.encrypteddisk_set.all():
            dev = ed.encrypted_provider
            proc = self.__pipeopen("geli delkey -n %d %s" % (
                slot,
                dev,
                ))

            err = proc.communicate()[1]
            if proc.returncode != 0:
                raise MiddlewareError("Unable to remove key: %s" % (err, ))

    def geli_is_decrypted(self, dev):
        doc = self.__geom_confxml()
        geom = doc.xpathEval("//class[name = 'ELI']/geom[name = '%s.eli']" % (
            dev,
            )
            )
        if geom:
            return True
        return False

    def geli_attach_single(self, prov, key, passphrase=None):
        if not passphrase:
            _passphrase = "-p"
        else:
            _passphrase = "-j %s" % passphrase
        proc = self.__pipeopen("geli attach %s -k %s %s" % (
            _passphrase,
            key,
            prov,
            ))
        proc.communicate()
        if os.path.exists("/dev/%s.eli" % prov):
            return True
        return False

    def geli_attach(self, volume, passphrase=None, key=None):
        """
        Attach geli providers of a given volume

        Returns the number of providers that failed to attach
        """
        failed = 0
        if key is None:
            geli_keyfile = volume.get_geli_keyfile()
        else:
            geli_keyfile = key
        if not passphrase:
            _passphrase = "-p"
        else:
            _passphrase = "-j %s" % passphrase
        for ed in volume.encrypteddisk_set.all():
            dev = ed.encrypted_provider
            proc = self.__pipeopen("geli attach %s -k %s %s" % (
                _passphrase,
                geli_keyfile,
                dev,
            ))
            err = proc.communicate()[1]
            if proc.returncode != 0:
                log.warn("Failed to geli attach %s: %s", dev, err)
                failed += 1
        return failed

    def geli_testkey(self, volume, passphrase=None):
        """
        Test key for geli providers of a given volume
        """

        assert volume.vol_fstype == 'ZFS'

        geli_keyfile = volume.get_geli_keyfile()
        if not passphrase:
            _passphrase = "-p"
        else:
            _passphrase = "-j %s" % passphrase

        """
        Parse zpool status to get encrypted providers
        EncryptedDisk table might be out of sync for some reason,
        this is much more reliable!
        """
        zpool = self.zpool_parse(volume.vol_name)
        for dev in zpool.get_devs():
            if not dev.name.endswith(".eli"):
                continue
            proc = self.__pipeopen("geli attach %s -k %s %s" % (
               _passphrase,
               geli_keyfile,
               dev.name.replace(".eli", ""),
               ))
            err = proc.communicate()[1]
            if err.find('Wrong key') != -1:
                return False
        return True

    def geli_detach(self, dev):
        """
        Detach geli provider

        Returns false if a device suffixed with .eli exists at the end of
        the operation and true otherwise
        """
        proc = self.__pipeopen("geli detach %s" % (
            dev,
            ))
        err = proc.communicate()[1]
        if proc.returncode != 0:
            log.warn("Failed to geli detach %s: %s", dev, err)
        if os.path.exists("/dev/%s.eli"):
            return False
        return True

    def geli_get_all_providers(self):
        """
        Get all unused geli providers

        It might be an entire disk or a partition of type freebsd-zfs
        (GELI on UFS not supported yet)
        """
        providers = []
        doc = self.__geom_confxml()
        disks = self.get_disks()
        for disk in disks:
            parts = [node.content for node in doc.xpathEval("//class[name = 'PART']/geom[name = '%s']/provider/config[type = 'freebsd-zfs']/../name" % disk)]
            if not parts:
                parts = [disk]
            for part in parts:
                proc = self.__pipeopen("geli dump %s" % part)
                proc.communicate()
                if proc.returncode == 0:
                    gptid = doc.xpathEval("//class[name = 'LABEL']/geom[name = '%s']/provider/name" % part)
                    if gptid:
                        providers.append((gptid[0].content, part))
                    else:
                        providers.append((part, part))
        return providers

    def __prepare_zfs_vdev(self, disks, swapsize, encrypt, volume):
        vdevs = []
        for disk in disks:
            self.__gpt_labeldisk(type = "freebsd-zfs",
                                 devname = disk,
                                 swapsize=swapsize)

        doc = self.__geom_confxml()
        for disk in disks:
            devname = self.part_type_from_device('zfs', disk)
            if encrypt:
                uuid = doc.xpathEval("//class[name = 'PART']"
                    "/geom//provider[name = '%s']/config/rawuuid" % (devname, )
                    )
                if not uuid:
                    log.warn("Could not determine GPT uuid for %s", devname)
                    raise MiddlewareError('Unable to determine GPT UUID for %s' % devname)
                else:
                    devname = self.__encrypt_device("gptid/%s" % uuid[0].content, disk, volume)
            else:
                uuid = doc.xpathEval("//class[name = 'PART']"
                    "/geom//provider[name = '%s']/config/rawuuid" % (devname, )
                    )
                if not uuid:
                    log.warn("Could not determine GPT uuid for %s", devname)
                    devname = "/dev/%s" % devname
                else:
                    devname = "/dev/gptid/%s" % uuid[0].content
            vdevs.append(devname)

        return vdevs

    def __create_zfs_volume(self, volume, swapsize, groups, path=None, init_rand=False):
        """Internal procedure to create a ZFS volume identified by volume id"""
        z_name = str(volume.vol_name)
        z_vdev = ""
        encrypt = (volume.vol_encrypt >= 1)
        # Grab all disk groups' id matching the volume ID
        self.__system("swapoff -a")
        device_list = []

        for vgrp in groups.values():
            vgrp_type = vgrp['type']
            if vgrp_type != 'stripe':
                z_vdev += " " + vgrp_type
            if vgrp_type in ('cache', 'log'):
                vdev_swapsize = 0
            else:
                vdev_swapsize = swapsize
            # Prepare disks nominated in this group
            vdevs = self.__prepare_zfs_vdev(vgrp['disks'], vdev_swapsize, encrypt, volume)
            z_vdev += " ".join([''] + vdevs)
            device_list += vdevs

        # Initialize devices with random data
        if init_rand:
            random_wipe(device_list)

        # Finally, create the zpool.
        # TODO: disallowing cachefile may cause problem if there is
        # preexisting zpool having the exact same name.
        if not os.path.isdir("/data/zfs"):
            os.makedirs("/data/zfs")

        altroot = 'none' if path else '/mnt'
        mountpoint = path if path else ('/%s' % (z_name, ))
        p1 = self.__pipeopen("zpool create -o cachefile=/data/zfs/zpool.cache "
                      "-o failmode=continue "
                      "-o autoexpand=on "
                      "-O aclmode=passthrough -O aclinherit=passthrough "
                      "-f -m %s -o altroot=%s %s %s" % (mountpoint, altroot, z_name, z_vdev))
        if p1.wait() != 0:
            error = ", ".join(p1.communicate()[1].split('\n'))
            raise MiddlewareError('Unable to create the pool: %s' % error)

        #We've our pool, lets retrieve the GUID
        p1 = self.__pipeopen("zpool get guid %s" % z_name)
        if p1.wait() == 0:
            line = p1.communicate()[0].split('\n')[1].strip()
            volume.vol_guid = re.sub('\s+', ' ', line).split(' ')[2]
            volume.save()
        else:
            log.warn("The guid of the pool %s could not be retrieved", z_name)

        self.zfs_inherit_option(z_name, 'mountpoint')

        self.__system("zpool set cachefile=/data/zfs/zpool.cache %s" % (z_name))
        #TODO: geli detach -l

    def zfs_volume_attach_group(self, volume, group, encrypt=False):
        """Attach a disk group to a zfs volume"""

        vgrp_type = group['type']
        if vgrp_type in ('log', 'cache'):
            swapsize = 0
        else:
            c = self.__open_db()
            c.execute("SELECT adv_swapondrive FROM system_advanced ORDER BY -id LIMIT 1")
            swapsize = c.fetchone()[0]

        assert volume.vol_fstype == 'ZFS'
        z_name = volume.vol_name
        z_vdev = ""
        encrypt = (volume.vol_encrypt >= 1)

        # FIXME swapoff -a is overkill
        self.__system("swapoff -a")
        if vgrp_type != 'stripe':
            z_vdev += " " + vgrp_type

        # Prepare disks nominated in this group
        vdevs = self.__prepare_zfs_vdev(group['disks'], swapsize, encrypt, volume)
        z_vdev += " ".join([''] + vdevs)

        # Finally, attach new groups to the zpool.
        self.__system("zpool add -f %s %s" % (z_name, z_vdev))
        #TODO: geli detach -l
        self._reload_disk()

    def create_zfs_vol(self, name, size, props=None, sparse=False):
        """Internal procedure to create ZFS volume"""
        if sparse is True:
            options = "-s "
        else:
            options = " "
        if props:
            assert type(props) is types.DictType
            for k in props.keys():
                if props[k] != 'inherit':
                    options += "-o %s=%s " % (k, props[k])
        zfsproc = self.__pipeopen("/sbin/zfs create %s -V %s %s" % (options, size, name))
        zfs_err = zfsproc.communicate()[1]
        zfs_error = zfsproc.wait()
        return zfs_error, zfs_err

    def create_zfs_dataset(self, path, props=None):
        """Internal procedure to create ZFS volume"""
        options = " "
        if props:
            assert type(props) is types.DictType
            for k in props.keys():
                if props[k] != 'inherit':
                    options += "-o %s=%s " % (k, props[k])
        zfsproc = self.__pipeopen("/sbin/zfs create %s %s" % (options, path))
        zfs_output, zfs_err = zfsproc.communicate()
        zfs_error = zfsproc.wait()
        if zfs_error == 0:
            self.restart("collectd")
        return zfs_error, zfs_err

    def list_zfs_vols(self, volname):
        """Return a dictionary that contains all ZFS volumes list"""
        zfsproc = self.__pipeopen("/sbin/zfs list -H -o name,volsize,used,avail,refer -t volume -r %s" % (str(volname),))
        zfs_output, zfs_err = zfsproc.communicate()
        zfs_output = zfs_output.split('\n')
        retval = {}
        for line in zfs_output:
            if line == "":
                continue
            data = line.split('\t')
            retval[data[0]] = {
                'volsize': data[1],
                'used': data[2],
                'avail': data[3],
                'refer': data[4],
            }
        return retval

    def list_zfs_fsvols(self):
        proc = self.__pipeopen("/sbin/zfs list -H -o name -t volume,filesystem")
        out, err = proc.communicate()
        out = out.split('\n')
        retval = OrderedDict()
        if proc.returncode == 0:
            for line in out:
                if not line:
                    continue
                retval[line] = line
        return retval

    def __snapshot_hold(self, name):
        """
        Check if a given snapshot is being hold by the replication system
        DISCLAIMER: mntlock has to be acquired before this call
        """
        zfsproc = self.__pipeopen("zfs get -H freenas:state %s" % (name))
        output = zfsproc.communicate()[0]
        if output != '':
            fsname, attrname, value, source = output.split('\n')[0].split('\t')
            if value != '-' and value != 'NEW':
                return True
        return False

    def destroy_zfs_dataset(self, path, recursive=False):
        retval = None
        if '@' in path:
            try:
                with mntlock(blocking=False):
                    if self.__snapshot_hold(path):
                        retval = 'Held by replication system.'
            except IOError:
                retval = 'Try again later.'
        elif recursive:
            try:
                with mntlock(blocking=False):
                    zfsproc = self.__pipeopen("/sbin/zfs list -Hr -t snapshot -o name %s" % (path))
                    snaps = zfsproc.communicate()[0]
                    for snap in filter(None, snaps.splitlines()):
                        if self.__snapshot_hold(snap):
                            retval = '%s: Held by replication system.' % snap
                            break
            except IOError:
                retval = 'Try again later.'
        if retval is None:
            if recursive:
                zfsproc = self.__pipeopen("zfs destroy -r %s" % (path))
            else:
                zfsproc = self.__pipeopen("zfs destroy %s" % (path))
            retval = zfsproc.communicate()[1]
            if zfsproc.returncode == 0:
                from freenasUI.storage.models import Task, Replication
                Task.objects.filter(task_filesystem=path).delete()
                Replication.objects.filter(repl_filesystem=path).delete()
        if not retval:
            try:
                self.__rmdir_mountpoint(path)
            except MiddlewareError as me:
                retval = str(me)

        return retval

    def destroy_zfs_vol(self, name):
        zfsproc = self.__pipeopen("zfs destroy %s" % (str(name),))
        retval = zfsproc.communicate()[1]
        return retval

    def __destroy_zfs_volume(self, volume):
        """Internal procedure to destroy a ZFS volume identified by volume id"""
        vol_name = str(volume.vol_name)
        # First, destroy the zpool.
        disks = volume.get_disks()
        self.__system("zpool destroy -f %s" % (vol_name, ))

        # Clear out disks associated with the volume
        for disk in disks:
            self.__gpt_unlabeldisk(devname=disk)

    def __create_ufs_volume(self, volume, swapsize, group):
        geom_vdev = ""
        u_name = str(volume.vol_name)
        # TODO: We do not support multiple GEOM levels for now.
        geom_type = group['type']

        if geom_type == '':
            # Grab disk from the group
            disk = group['disks'][0]
            self.__gpt_labeldisk(type = "freebsd-ufs", devname = disk, swapsize=swapsize)
            devname = self.part_type_from_device('ufs', disk)
            # TODO: Need to investigate why /dev/gpt/foo can't have label /dev/ufs/bar
            # generated automatically
            p1 = self.__pipeopen("newfs -U -L %s /dev/%s" % (u_name, devname))
            stderr = p1.communicate()[1]
            if p1.returncode != 0:
                error = ", ".join(stderr.split('\n'))
                raise MiddlewareError('Volume creation failed: "%s"' % error)
        else:
            # Grab all disks from the group
            for disk in group['disks']:
                # FIXME: turn into a function
                self.__system("dd if=/dev/zero of=/dev/%s bs=1m count=1" % (disk,))
                self.__system("dd if=/dev/zero of=/dev/%s bs=1m oseek=`diskinfo %s "
                      "| awk '{print int($3 / (1024*1024)) - 4;}'`" % (disk, disk))
                geom_vdev += " /dev/" + disk
                #TODO gpt label disks
            self.__system("geom %s load" % (geom_type))
            p1 = self.__pipeopen("geom %s label %s %s" % (geom_type, volume.vol_name, geom_vdev))
            stdout, stderr = p1.communicate()
            if p1.returncode != 0:
                error = ", ".join(stderr.split('\n'))
                raise MiddlewareError('Volume creation failed: "%s"' % error)
            ufs_device = "/dev/%s/%s" % (geom_type, volume.vol_name)
            self.__system("newfs -U -L %s %s" % (u_name, ufs_device))

    def __destroy_ufs_volume(self, volume):
        """Internal procedure to destroy a UFS volume identified by volume id"""
        u_name = str(volume.vol_name)

        disks = volume.get_disks()
        provider = self.get_label_consumer('ufs', u_name)
        if not provider:
            return None
        geom_type = provider.xpathEval("../../name")[0].content.lower()

        if geom_type not in ('mirror', 'stripe', 'raid3'):
            # Grab disk from the group
            disk = disks[0]
            self.__system("umount -f /dev/ufs/" + u_name)
            self.__gpt_unlabeldisk(devname = disk)
        else:
            g_name = provider.xpathEval("../name")[0].content
            self.__system("swapoff -a")
            self.__system("umount -f /dev/ufs/" + u_name)
            self.__system("geom %s stop %s" % (geom_type, g_name))
            # Grab all disks from the group
            for disk in disks:
                self.__system("geom %s clear %s" % (geom_type, disk))
                self.__system("dd if=/dev/zero of=/dev/%s bs=1m count=1" % (disk,))
                self.__system("dd if=/dev/zero of=/dev/%s bs=1m oseek=`diskinfo %s "
                      "| awk '{print int($3 / (1024*1024)) - 4;}'`" % (disk, disk))

    def _init_volume(self, volume, *args, **kwargs):
        """Initialize a volume designated by volume_id"""
        c = self.__open_db()
        c.execute("SELECT adv_swapondrive FROM system_advanced ORDER BY -id LIMIT 1")
        swapsize=c.fetchone()[0]
        c.close()

        assert volume.vol_fstype == 'ZFS' or volume.vol_fstype == 'UFS'
        if volume.vol_fstype == 'ZFS':
            self.__create_zfs_volume(volume, swapsize, kwargs.pop('groups', False), kwargs.pop('path', None), init_rand=kwargs.pop('init_rand', False))
        elif volume.vol_fstype == 'UFS':
            self.__create_ufs_volume(volume, swapsize, kwargs.pop('groups')['root'])

    def zfs_replace_disk(self, volume, from_label, to_disk, passphrase=None):
        """Replace disk in zfs called `from_label` to `to_disk`"""
        from freenasUI.storage.models import Disk, EncryptedDisk
        c = self.__open_db()
        c.execute("SELECT adv_swapondrive FROM system_advanced ORDER BY -id LIMIT 1")
        swapsize = c.fetchone()[0]

        assert volume.vol_fstype == 'ZFS'

        # TODO: Test on real hardware to see if ashift would persist across replace
        from_disk = self.label_to_disk(from_label)
        from_swap = self.part_type_from_device('swap', from_disk)
        encrypt = (volume.vol_encrypt >= 1)

        if from_swap != '':
            self.__system('/sbin/swapoff /dev/%s.eli' % (from_swap, ))
            self.__system('/sbin/geli detach /dev/%s' % (from_swap, ))

        # to_disk _might_ have swap on, offline it before gpt label
        to_swap = self.part_type_from_device('swap', to_disk)
        if to_swap != '':
            self.__system('/sbin/swapoff /dev/%s.eli' % (to_swap, ))
            self.__system('/sbin/geli detach /dev/%s' % (to_swap, ))

        # Replace in-place
        if from_disk == to_disk:
            self.__system('/sbin/zpool offline %s %s' % (volume.vol_name, from_label))

        self.__gpt_labeldisk(type="freebsd-zfs", devname=to_disk, swapsize=swapsize)

        # There might be a swap after __gpt_labeldisk
        to_swap = self.part_type_from_device('swap', to_disk)
        # It has to be a freebsd-zfs partition there
        to_label = self.part_type_from_device('zfs', to_disk)

        if to_label == '':
            raise MiddlewareError('freebsd-zfs partition could not be found')

        doc = self.__geom_confxml()
        uuid = doc.xpathEval("//class[name = 'PART']"
            "/geom//provider[name = '%s']/config/rawuuid" % (to_label, )
            )
        if not encrypt:
            if not uuid:
                log.warn("Could not determine GPT uuid for %s", to_label)
                devname = to_label
            else:
                devname = "gptid/%s" % uuid[0].content
        else:
            if not uuid:
                log.warn("Could not determine GPT uuid for %s", to_label)
                raise MiddlewareError('Unable to determine GPT UUID for %s' % devname)
            else:
                from_diskobj = Disk.objects.filter(disk_name=from_disk, disk_enabled=True)
                if from_diskobj.exists():
                    ed = EncryptedDisk.objects.filter(encrypted_volume=volume, encrypted_disk=from_diskobj[0]).delete()
                devname = self.__encrypt_device("gptid/%s" % uuid[0].content, to_disk, volume, passphrase=passphrase)

        if from_disk == to_disk:
            self.__system('/sbin/zpool online %s %s' % (volume.vol_name, to_label))
            ret = self.__system_nolog('/sbin/zpool replace %s %s' % (volume.vol_name, to_label))
            if ret == 256:
                ret = self.__system_nolog('/sbin/zpool scrub %s' % (volume.vol_name))
        else:
            p1 = self.__pipeopen('/sbin/zpool replace %s %s %s' % (volume.vol_name, from_label, devname))
            stdout, stderr = p1.communicate()
            ret = p1.returncode
            if ret != 0:
                if from_swap != '':
                    self.__system('/sbin/geli onetime /dev/%s' % (from_swap))
                    self.__system('/sbin/swapon /dev/%s.eli' % (from_swap))
                error = ", ".join(stderr.split('\n'))
                if to_swap != '':
                    self.__system('/sbin/swapoff /dev/%s.eli' % (to_swap, ))
                    self.__system('/sbin/geli detach /dev/%s' % (to_swap, ))
                if encrypt:
                    self.__system('/sbin/geli detach %s' % (devname, ))
                raise MiddlewareError('Disk replacement failed: "%s"' % error)
            #TODO: geli detach -l

        if to_swap:
            self.__system('/sbin/geli onetime /dev/%s' % (to_swap))
            self.__system('/sbin/swapon /dev/%s.eli' % (to_swap))

        return ret

    def zfs_offline_disk(self, volume, label):
        from freenasUI.storage.models import EncryptedDisk

        assert volume.vol_fstype == 'ZFS'

        # TODO: Test on real hardware to see if ashift would persist across replace
        disk = self.label_to_disk(label)
        swap = self.part_type_from_device('swap', disk)

        if swap != '':
            self.__system('/sbin/swapoff /dev/%s.eli' % (swap, ))
            self.__system('/sbin/geli detach /dev/%s' % (swap, ))

        # Replace in-place
        p1 = self.__pipeopen('/sbin/zpool offline %s %s' % (volume.vol_name, label))
        stderr = p1.communicate()[1]
        if p1.returncode != 0:
            error = ", ".join(stderr.split('\n'))
            raise MiddlewareError('Disk offline failed: "%s"' % error)
        if label.endswith(".eli"):
            self.__system("/sbin/geli detach /dev/%s" % label)
            EncryptedDisk.objects.filter(
                encrypted_volume=volume,
                encrypted_provider=label[:-4]
            ).delete()

    def zfs_detach_disk(self, volume, label):
        """Detach a disk from zpool
           (more technically speaking, a replaced disk.  The replacement actually
           creates a mirror for the device to be replaced)"""

        assert volume.vol_fstype == 'ZFS'

        from_disk = self.label_to_disk(label)
        if not from_disk:
            if not re.search(r'^[0-9]+$', label):
                log.warn("Could not find disk for the ZFS label %s", label)
        else:
            from_swap = self.part_type_from_device('swap', from_disk)

            # Remove the swap partition for another time to be sure.
            # TODO: swap partition should be trashed instead.
            if from_swap != '':
                self.__system('/sbin/swapoff /dev/%s.eli' % (from_swap,))
                self.__system('/sbin/geli detach /dev/%s' % (from_swap,))

        ret = self.__system_nolog('/sbin/zpool detach %s %s' % (volume.vol_name, label))
        if from_disk:
            # TODO: This operation will cause damage to disk data which should be limited
            self.__gpt_unlabeldisk(from_disk)
        return ret

    def zfs_remove_disk(self, volume, label):
        """
        Remove a disk from zpool
        Cache disks, inactive hot-spares (and log devices in zfs 28) can be removed
        """

        assert volume.vol_fstype == 'ZFS'

        from_disk = self.label_to_disk(label)
        from_swap = self.part_type_from_device('swap', from_disk)

        if from_swap != '':
            self.__system('/sbin/swapoff /dev/%s.eli' % (from_swap,))
            self.__system('/sbin/geli detach /dev/%s' % (from_swap,))

        p1 = self.__pipeopen('/sbin/zpool remove %s %s' % (volume.vol_name, label))
        stderr = p1.communicate()[1]
        if p1.returncode != 0:
            error = ", ".join(stderr.split('\n'))
            raise MiddlewareError('Disk could not be removed: "%s"' % error)
        # TODO: This operation will cause damage to disk data which should be limited

        if from_disk:
            self.__gpt_unlabeldisk(from_disk)

    def detach_volume_swaps(self, volume):
        """Detach all swaps associated with volume"""
        disks = volume.get_disks()
        for disk in disks:
            swapdev = self.part_type_from_device('swap', disk)
            if swapdev != '':
                self.__system("swapoff /dev/%s.eli" % swapdev)
                self.__system("geli detach /dev/%s" % swapdev)

    def __get_mountpath(self, name, fstype, mountpoint_root='/mnt'):
        """Determine the mountpoint for a volume or ZFS dataset

        It tries to divine the location of the volume or dataset from the
        relevant command, and if all else fails, falls back to a less
        elegant method of representing the mountpoint path.

        This is done to ensure that in the event that the database and
        reality get out of synch, the user can nuke the volume/mountpoint.

        XXX: this should be done more elegantly by calling getfsent from C.

        Required Parameters:
            name: textual name for the mountable vdev or volume, e.g. 'tank',
                  'stripe', 'tank/dataset', etc.
            fstype: filesystem type for the vdev or volume, e.g. 'UFS', 'ZFS',
                    etc.

        Optional Parameters:
            mountpoint_root: the root directory where all of the datasets and
                             volumes shall be mounted. Defaults to '/mnt'.

        Returns:
            the absolute path for the volume on the system.
        """
        if fstype == 'ZFS':
            p1 = self.__pipeopen('zfs list -H -o mountpoint %s' % (name, ))
            stdout = p1.communicate()[0]
            if not p1.returncode:
                return stdout.strip()
        elif fstype == 'UFS':
            p1 = self.__pipeopen('mount -p')
            stdout = p1.communicate()[0]
            if not p1.returncode:
                flines = filter(lambda x: x and x.split()[0] == \
                                                '/dev/ufs/' + name,
                                stdout.splitlines())
                if flines:
                    return flines[0].split()[1]

        return os.path.join(mountpoint_root, name)

    def _destroy_volume(self, volume):
        """Destroy a volume on the system

        This either destroys a zpool or umounts a generic volume (e.g. NTFS,
        UFS, etc) and nukes it.

        In the event that the volume is still in use in the OS, the end-result
        is implementation defined depending on the filesystem, and the set of
        commands used to export the filesystem.

        Finally, this method goes and cleans up the mountpoint, as it's
        assumed to be no longer needed. This is also a sanity check to ensure
        that cleaning up everything worked.

        XXX: doing recursive unmounting here might be a good idea.
        XXX: better feedback about files in use might be a good idea...
             someday. But probably before getting to this point. This is a
             tricky problem to fix in a way that doesn't unnecessarily suck up
             resources, but also ensures that the user is provided with
             meaningful data.
        XXX: divorce this from storage.models; depending on storage.models
             introduces a circular dependency and creates design ugliness.
        XXX: implement destruction algorithm for non-UFS/-ZFS.

        Parameters:
            volume: a storage.models.Volume object.

        Raises:
            MiddlewareError: the volume could not be detached cleanly.
            MiddlewareError: the volume's mountpoint couldn't be removed.
            ValueError: 'destroy' isn't implemented for the said filesystem.
        """

        # volume_detach compatibility.
        vol_name, vol_fstype = volume.vol_name, volume.vol_fstype

        vol_mountpath = self.__get_mountpath(vol_name, vol_fstype)

        if vol_fstype == 'ZFS':
            self.__destroy_zfs_volume(volume)
        elif vol_fstype == 'UFS':
            self.__destroy_ufs_volume(volume)
        else:
            raise ValueError("destroy isn't implemented for the %s filesystem"
                             % (vol_fstype, ))

        self._reload_disk()
        self.__rmdir_mountpoint(vol_mountpath)

    def _reload_disk(self):
        self.__system("/usr/sbin/service ix-fstab quietstart")
        self.__system("/usr/sbin/service encswap quietstart")
        self.__system("/usr/sbin/service swap1 quietstart")
        self.__system("/usr/sbin/service mountlate quietstart")
        self.restart("collectd")
        self.__confxml = None

    # Create a user in system then samba
    def __pw_with_password(self, command, password):
        pw = self.__pipeopen(command)
        msg = pw.communicate("%s\n" % password)[1]
        if pw.returncode != 0:
            raise MiddlewareError("Operation could not be performed. %s" % msg)

        if msg != "":
            log.debug("Command reports %s", msg)

    def __smbpasswd(self, username, password):
        """
        Add the user ``username'' to samba using ``password'' as
        the current password

        Returns:
            True whether the user has been successfully added and False otherwise
        """
        command = '/usr/local/bin/smbpasswd -D 0 -s -a "%s"' % (username)
        smbpasswd = self.__pipeopen(command)
        smbpasswd.communicate("%s\n%s\n" % (password, password))
        return smbpasswd.returncode == 0

    def __issue_pwdchange(self, username, command, password):
        self.__pw_with_password(command, password)
        self.__smbpasswd(username, password)

    def user_create(self, username, fullname, password, uid=-1, gid=-1,
                    shell="/sbin/nologin",
                    homedir='/mnt', homedir_mode=0o755,
                    password_disabled=False):
        """Create a user.

        This goes and compiles the invocation needed to execute via pw(8),
        then goes and creates a home directory. Then it goes and adds the
        user via pw(8), and finally adds the user's to the samba user
        database. If adding the user fails for some reason, it will remove
        the directory.

        Required parameters:

        username - a textual identifier for the user (should conform to
                   all constraints with Windows, Unix and OSX usernames).
                   Example: 'root'.
        fullname - a textual 'humanized' identifier for the user. Example:
                   'Charlie Root'.
        password - passphrase used to login to the system; this is
                   ignored if password_disabled is True.

        Optional parameters:

        uid - uid for the user. Defaults to -1 (defaults to the next UID
              via pw(8)).
        gid - gid for the user. Defaults to -1 (defaults to the next GID
              via pw(8)).
        shell - login shell for a user when logging in interactively.
                Defaults to /sbin/nologin.
        homedir - where the user will be put, or /nonexistent if
                  the user doesn't need a directory; defaults to /mnt.
        homedir_mode - mode to use when creating the home directory;
                       defaults to 0755.
        password_disabled - should password based logins be allowed for 
                            the user? Defaults to False.

        XXX: the default for the home directory seems like a bad idea.
             Should this be a required parameter instead, or default
             to /var/empty?
        XXX: seems like the password_disabled and password fields could
             be rolled into one property.
        XXX: the homedir mode isn't set today by the GUI; the default
             is set to the FreeBSD default when calling pw(8).
        XXX: smbpasswd errors aren't being caught today.
        XXX: invoking smbpasswd for each user add seems like an
             expensive operation.
        XXX: why are we returning the password hashes?

        Returns:
            A tuple of the user's UID, GID, the Unix encrypted password
            hash, and the encrypted SMB password hash.

        Raises:
            MiddlewareError - tried to create a home directory under a
                              subdirectory on the /mnt memory disk.
            MiddlewareError - failed to create the home directory for
                              the user.
            MiddlewareError - failed to run pw useradd successfully.
        """
        command = '/usr/sbin/pw useradd "%s" -o -c "%s" -d "%s" -s "%s"' % \
            (username, fullname, homedir, shell, )
        if password_disabled:
            command += ' -h -'
        else:
            command += ' -h 0'
        if uid >= 0:
            command += " -u %d" % (uid)
        if gid >= 0:
            command += " -g %d" % (gid)
        if homedir != '/nonexistent':
            # Populate the home directory with files from /usr/share/skel .
            command += ' -m'

        # Is this a new directory or not? Let's not nuke existing directories,
        # e.g. /, /root, /mnt/tank/my-dataset, etc ;).
        new_homedir = False

        if homedir != '/nonexistent':
            # Kept separate for cleanliness between formulating what to do
            # and executing the formulated plan.

            # You're probably wondering why pw -m doesn't suffice. Here's why:
            # 1. pw(8) doesn't create home directories if the base directory
            #    doesn't exist; example: if /mnt/tank/homes doesn't exist and
            #    the user specified /mnt/tank/homes/user, then the home
            #    directory won't be created.
            # 2. pw(8) allows me to specify /mnt/md_size (a regular file) for
            #    the home directory.
            # 3. If some other random path creation error occurs, it's already
            #    too late to roll back the user create.
            try:
                os.makedirs(homedir, mode=homedir_mode)
                if os.stat(homedir).st_dev == os.stat('/mnt').st_dev:
                    # HACK: ensure the user doesn't put their homedir under
                    # /mnt
                    # XXX: fix the GUI code and elsewhere to enforce this, then
                    # remove the hack.
                    raise MiddlewareError('Path for the home directory (%s) '
                                          'must be under a volume or dataset'
                                          % (homedir, ))
            except OSError as oe:
                if oe.errno == errno.EEXIST:
                    if not os.path.isdir(homedir):
                        raise MiddlewareError('Path for home directory already '
                                              'exists and is not a directory')
                else:
                    raise MiddlewareError('Failed to create the home directory '
                                          '(%s) for user: %s'
                                          % (homedir, str(oe)))
            else:
                new_homedir = True

        try:
            self.__issue_pwdchange(username, command, password)
            """
            Make sure to use -d 0 for pdbedit, otherwise it will bomb
            if CIFS debug level is anything different than 'Minimum'
            """
            smb_command = "/usr/local/bin/pdbedit -d 0 -w %s" % username
            smb_cmd = self.__pipeopen(smb_command)
            smb_hash = smb_cmd.communicate()[0].split('\n')[0]
        except:
            if new_homedir:
                # Be as atomic as possible when creating the user if
                # commands failed to execute cleanly.
                shutil.rmtree(homedir)
            raise

        user = self.___getpwnam(username)
        return (user.pw_uid, user.pw_gid, user.pw_passwd, smb_hash)

    def user_lock(self, username):
        self.__system('/usr/local/bin/smbpasswd -d "%s"' % (username))
        self.__system('/usr/sbin/pw lock "%s"' % (username))
        return self.user_gethashedpassword(username)

    def user_unlock(self, username):
        self.__system('/usr/local/bin/smbpasswd -e "%s"' % (username))
        self.__system('/usr/sbin/pw unlock "%s"' % (username))
        return self.user_gethashedpassword(username)

    def user_changepassword(self, username, password):
        """Changes user password"""
        command = '/usr/sbin/pw usermod "%s" -h 0' % (username)
        self.__issue_pwdchange(username, command, password)
        return self.user_gethashedpassword(username)

    def user_gethashedpassword(self, username):
        """
        Get the samba hashed password for ``username''

        Returns:
            tuple -> (user password, samba hash)
        """

        """
        Make sure to use -d 0 for pdbedit, otherwise it will bomb
        if CIFS debug level is anything different than 'Minimum'
        """
        smb_command = "/usr/local/bin/pdbedit -d 0 -w %s" % username
        smb_cmd = self.__pipeopen(smb_command)
        smb_hash = smb_cmd.communicate()[0].split('\n')[0]
        user = self.___getpwnam(username)
        return (user.pw_passwd, smb_hash)

    def user_deleteuser(self, username):
        """
        Delete a user using pw(8) utility

        Returns:
            bool
        """
        pipe = self.__pipeopen('/usr/sbin/pw userdel "%s"' % (username, ))
        err = pipe.communicate()[1]
        if pipe.returncode != 0:
            log.warn("Failed to delete user %s: %s", username, err)
            return False
        return True

    def user_deletegroup(self, groupname):
        """
        Delete a group using pw(8) utility

        Returns:
            bool
        """
        pipe = self.__pipeopen('/usr/sbin/pw groupdel "%s"' % (groupname, ))
        err = pipe.communicate()[1]
        if pipe.returncode != 0:
            log.warn("Failed to delete group %s: %s", groupname, err)
            return False
        return True

    def user_getnextuid(self):
        command = "/usr/sbin/pw usernext"
        pw = self.__pipeopen(command)
        uid = pw.communicate()[0]
        if pw.returncode != 0:
            raise ValueError("Could not retrieve usernext")
        uid = uid.split(':')[0]
        return uid

    def user_getnextgid(self):
        command = "/usr/sbin/pw groupnext"
        pw = self.__pipeopen(command)
        gid = pw.communicate()[0]
        if pw.returncode != 0:
            raise ValueError("Could not retrieve groupnext")
        return gid

    def save_pubkey(self, homedir, pubkey, username, groupname):
        homedir = str(homedir)
        pubkey = str(pubkey).strip()
        if pubkey:
            pubkey = '%s\n' % pubkey
        sshpath = '%s/.ssh' % (homedir)
        keypath = '%s/.ssh/authorized_keys' % (homedir)
        try:
            oldpubkey = open(keypath).read()
            if oldpubkey == pubkey:
                return
        except:
            pass

        if homedir == '/root':
            self.__system("/sbin/mount -uw -o noatime /")
        saved_umask = os.umask(077)
        if not os.path.isdir(sshpath):
            os.makedirs(sshpath)
        if not os.path.isdir(sshpath):
            return # FIXME: need better error reporting here
        if pubkey == '' and os.path.exists(keypath):
            os.unlink(keypath)
        else:
            fd = open(keypath, 'w')
            fd.write(pubkey)
            fd.close()
            self.__system("/usr/sbin/chown -R %s:%s %s" % (username, groupname, sshpath))
        if homedir == '/root':
            self.__system("/sbin/mount -ur /")
        os.umask(saved_umask)

    def _reload_user(self):
        self.__system("/usr/sbin/service ix-passwd quietstart")
        self.__system("/usr/sbin/service ix-aliases quietstart")
        self.reload("cifs")

    def mp_change_permission(self, path='/mnt', user='root', group='wheel',
                             mode='0755', recursive=False, acl='unix'):

        if type(group) is types.UnicodeType:
            group = group.encode('utf-8')

        if type(user) is types.UnicodeType:
            user = user.encode('utf-8')

        if type(mode) is types.UnicodeType:
            mode = mode.encode('utf-8')

        if type(path) is types.UnicodeType:
            path = path.encode('utf-8')

        winacl = os.path.join(path, ACL_WINDOWS_FILE)
        winexists = (ACL.get_acl_ostype(path) == ACL_FLAGS_OS_WINDOWS)
        if acl == 'windows' and not winexists:
            open(winacl, 'a').close()
            winexists = True
        elif acl == 'unix' and winexists:
            os.unlink(winacl)
            winexists = False

        if winexists:
            script = "/usr/local/www/freenasUI/tools/winacl.sh"
            args=" -o '%s' -g '%s' -d %s " % (user, group, mode)
            if recursive:
                args += " -r "
            args += " -p %s" % path
            cmd = "%s %s" % (script, args)
            self.__system(cmd)

        else:
            flags = ""
            if recursive:
                flags = "-R"
            self.__system("/usr/sbin/chown %s '%s':'%s' '%s'" % (flags, user, group, path))
            self.__system("/bin/chmod %s %s '%s'" % (flags, mode, path))

    def mp_get_permission(self, path):
        if os.path.isdir(path):
            return stat.S_IMODE(os.stat(path)[stat.ST_MODE])

    def mp_get_owner(self, path):
        """Gets the owner/group for a given mountpoint.

        Defaults to root:wheel if the owner of the mountpoint cannot be found.

        XXX: defaulting to root:wheel is wrong if the users/groups are out of
             synch with the remote hosts. These cases should really raise
             Exceptions and be handled differently in the GUI.

        Raises:
            OSError - the path provided isn't a directory.
        """
        if os.path.isdir(path):
            stat_info = os.stat(path)
            uid = stat_info.st_uid
            gid = stat_info.st_gid
            try:
                pw = pwd.getpwuid(uid)
                user = pw.pw_name
            except KeyError:
                user = 'root'
            try:
                gr = grp.getgrgid(gid)
                group = gr.gr_name
            except KeyError:
                group = 'wheel'
            return (user, group, )
        raise OSError('Invalid mountpoint %s' % (path, ))

    def change_upload_location(self, path):
        vardir = "/var/tmp/firmware"

        self.__system("/bin/rm -rf %s" % vardir)
        self.__system("/bin/mkdir -p %s/.freenas" % path)
        self.__system("/usr/sbin/chown www:www %s/.freenas" % path)
        self.__system("/bin/chmod 755 %s/.freenas" % path)
        self.__system("/bin/ln -s %s/.freenas %s" % (path, vardir))

    def create_upload_location(self):
        """
        Create a temporary location for firmware upgrade
        over a memory device (mdconfig) using UFS

        Raises:
            MiddlewareError
        """

        sw_name = get_sw_name()
        label = "%smdu" % (sw_name, )
        doc = self.__geom_confxml()

        pref = doc.xpathEval("//class[name = 'LABEL']/geom/"
            "provider[name = 'ufs/%s']/../consumer/provider/@ref" % (label, ))
        #prov = doc.xpathEval("//provider[@id = '%s']" % pref[0].content)
        if not pref:
            proc = self.__pipeopen("/sbin/mdconfig -a -t swap -s 2800m -o reserve")
            mddev, err = proc.communicate()
            if proc.returncode != 0:
                raise MiddlewareError("Could not create memory device: %s" % err)

            proc = self.__pipeopen("newfs -L %s /dev/%s" % (label, mddev))
            err = proc.communicate()[1]
            if proc.returncode != 0:
                raise MiddlewareError("Could not create temporary filesystem: %s" % err)

            self.__system("/bin/mkdir -p /var/tmp/firmware")
            proc = self.__pipeopen("mount /dev/ufs/%s /var/tmp/firmware" % (label, ))
            err = proc.communicate()[1]
            if proc.returncode != 0:
                raise MiddlewareError("Could not mount temporary filesystem: %s" % err)

        self.__system("/usr/sbin/chown www:www /var/tmp/firmware")
        self.__system("/bin/chmod 755 /var/tmp/firmware")

    def destroy_upload_location(self):
        """
        Destroy a temporary location for firmware upgrade
        over a memory device (mdconfig) using UFS

        Raises:
            MiddlewareError

        Returns:
            bool
        """

        sw_name = get_sw_name()
        label = "%smdu" % (sw_name, )
        doc = self.__geom_confxml()

        pref = doc.xpathEval("//class[name = 'LABEL']/geom/"
            "provider[name = 'ufs/%s']/../consumer/provider/@ref" % (label, ))
        if not pref:
            return False
        prov = doc.xpathEval("//class[name = 'MD']//provider[@id = '%s']/name" % pref[0].content)
        if not prov:
            return False

        mddev = prov[0].content

        self.__system("umount /dev/ufs/%s" % (label, ))
        proc = self.__pipeopen("mdconfig -d -u %s" % (mddev, ))
        err = proc.communicate()[1]
        if proc.returncode != 0:
            raise MiddlewareError("Could not destroy memory device: %s" % err)

        return True


    def validate_update(self, path):

        os.chdir(os.path.dirname(path))

        # XXX: ugly
        self.__system("rm -rf */")

        percent = 0
        with open('/tmp/.extract_progress', 'w') as fp:
            fp.write("2|%d\n" % percent)
            fp.flush()
            with open('/tmp/.upgrade_extract', 'w') as f:
                size = os.stat(path).st_size
                proc = subprocess.Popen([
                    "/usr/bin/tar",
                    "-xSJpf", # -S for sparse
                    path,
                ], stderr=f)
                RE_TAR = re.compile(r"^In: (\d+)", re.M|re.S)
                while True:
                    if proc.poll() is not None:
                        break
                    try:
                        os.kill(proc.pid, signal.SIGINFO)
                    except:
                        break
                    time.sleep(1)
                    #TODO: We don't need to read the whole file
                    with open('/tmp/.upgrade_extract', 'r') as f2:
                        line = f2.read()
                    reg = RE_TAR.findall(line)
                    if reg:
                        current = Decimal(reg[-1])
                        percent = (current / size ) * 100
                        fp.write("2|%d\n" % percent)
                        fp.flush()
            err = proc.communicate()[1]
            if proc.returncode != 0:
                os.chdir('/')
                raise MiddlewareError(
                    'The firmware image is invalid, make sure to use .txz file: %s' % err
                )
            fp.write("3|\n")
            fp.flush()
        os.unlink('/tmp/.extract_progress')
        try:
            subprocess.check_output(
                                    ['bin/install_worker.sh', 'pre-install'],
                                    stderr=subprocess.STDOUT,
                                    )
        except subprocess.CalledProcessError, cpe:
            raise MiddlewareError('The firmware does not meet the '
                                  'pre-install criteria: %s' % (cpe.output, ))
        finally:
            os.chdir('/')
        # XXX: bleh
        return True

    def apply_update(self, path):
        os.chdir(os.path.dirname(path))
        try:
            subprocess.check_output(
                                    ['bin/install_worker.sh', 'install'],
                                    stderr=subprocess.STDOUT,
                                    )
        except subprocess.CalledProcessError, cpe:
            raise MiddlewareError('The update failed: %s' % (str(cpe), ))
        finally:
            os.chdir('/')
            os.unlink(path)
        open(NEED_UPDATE_SENTINEL, 'w').close()

    def __umount_filesystems_within(self, path):
        """
        Try to umount filesystems within a certain path

        Raises:
            MiddlewareError - Could not umount
        """
        for mounted in get_mounted_filesystems():
            if mounted['fs_file'].startswith(path):
                if not umount(mounted['fs_file']):
                    raise MiddlewareError('Unable to umount %s' % (
                        mounted['fs_file'],
                        ))

    def get_plugin_upload_path(self):
        from freenasUI.jails.models import JailsConfiguration

        jc = JailsConfiguration.objects.order_by("-id")[0]
        plugin_upload_path = "%s/%s" % (jc.jc_path, ".plugins")

        if not os.path.exists(plugin_upload_path):
            self.__system("/bin/mkdir -p %s" % plugin_upload_path)
            self.__system("/usr/sbin/chown www:www %s" % plugin_upload_path)
            self.__system("/bin/chmod 755 %s" % plugin_upload_path)

        return plugin_upload_path

    def install_pbi(self, pjail, newplugin, pbifile="/var/tmp/firmware/pbifile.pbi"):
        log.debug("install_pbi: pjail = %s", pjail)
        """
        Install a .pbi file into the plugins jail

        Returns:
            bool: installation successful?

        Raises::
            MiddlewareError: pbi_add failed
        """
        from freenasUI.services.models import RPCToken
        from freenasUI.plugins.models import Plugins
        from freenasUI.jails.models import JailsConfiguration
        ret = False

        open('/tmp/.plugin_upload_install', 'w+').close()

        if not pjail:
            log.debug("install_pbi: pjail is NULL")
            return False

        if not self.pluginjail_running(pjail=pjail):
            log.debug("install_pbi: pjail is is not running")
            return False

        wjail = None
        wlist = Warden().list()
        for wj in wlist:
            wj = WardenJail(**wj)
            if wj.host  == pjail:
                wjail = wj
                break

        if wjail is None:
            raise MiddlewareError("The plugins jail is not running, start "
                "it before proceeding")

        jail = None
        for j in Jls():
            if j.hostname == wjail.host:
                jail = j
                break

        # this stuff needs better error checking.. .. ..
        if jail is None:
            raise MiddlewareError("The plugins jail is not running, start "
                "it before proceeding")

        jc = JailsConfiguration.objects.order_by("-id")[0]

        pjail_path = "%s/%s" % (jc.jc_path, wjail.host)
        plugins_path = "%s/%s" % (pjail_path, ".plugins")

        log.debug("install_pbi: pjail_path = %s, plugins_path = %s", pjail_path, plugins_path)

        pbi = pbiname = prefix = name = version = arch = None
        p = pbi_add(flags=PBI_ADD_FLAGS_INFO, pbi=pbifile)
        out = p.info(False, -1, 'pbi information for', 'prefix', 'name', 'version', 'arch')

        if not out:
            raise MiddlewareError("This file was not identified as in PBI "
                "format, it might as well be corrupt.")

        for pair in out:
            (var, val) = pair.split('=', 1)

            var = var.lower()
            if var == 'pbi information for':
                pbiname = val
                pbi = "%s.pbi" % val

            elif var == 'prefix':
                prefix = val

            elif var == 'name':
                name = val

            elif var == 'version':
                version = val

            elif var == 'arch':
                arch = val

        info = pbi_info(flags=PBI_INFO_FLAGS_VERBOSE)
        res = info.run(jail=True, jid=jail.jid)
        if res[0] == 0 and res[1]:
            plugins = re.findall(r'^Name: (?P<name>\w+)$', res[1], re.M)
            if name in plugins:
                #FIXME: do pbi_update instead
                pass

        if pbifile == "/var/tmp/firmware/pbifile.pbi":
            self.__system("/bin/mv /var/tmp/firmware/pbifile.pbi %s/%s" % (plugins_path, pbi))

        p = pbi_add(flags=PBI_ADD_FLAGS_NOCHECKSIG|PBI_ADD_FLAGS_FORCE, pbi="%s/%s" %
            ("/.plugins", pbi))
        res = p.run(jail=True, jid=jail.jid)
        if res and res[0] == 0:
            qs = Plugins.objects.filter(plugin_name=name)
            if qs.count() > 0:
		if qs[0].plugin_jail == pjail:
                    log.warn("Plugin named %s already exists in database, "
                        "overwriting.", name)
                    plugin = qs[0]
                else:
                    plugin = Plugins()
            else:
                plugin = Plugins()

            plugin.plugin_path = prefix
            plugin.plugin_enabled = True
            plugin.plugin_ip = jail.ip
            plugin.plugin_name = name
            plugin.plugin_arch = arch
            plugin.plugin_version = version
            plugin.plugin_pbiname = pbiname
            plugin.plugin_jail = wjail.host

            # icky, icky icky, this is how we roll though.
            port = 12345
            qs = Plugins.objects.order_by('-plugin_port')
            if qs.count() > 0:
                port = int(qs[0].plugin_port)

            plugin.plugin_port = port + 1

            """
            Check freenas file within pbi dir for settings
            Currently the API only looks for api_version
            """
            out = Jexec(jid=jail.jid, command="cat %s/freenas" % prefix).run()
            if out and out[0] == 0:
                for line in out[1].splitlines():
                    line = line.strip()
                    if not line:
                        continue

                    key, value = [i.strip() for i in line.split(':', 1)]
                    key = key.lower()
                    value = value.strip()
                    if key in ('api_version', ):
                        setattr(plugin, 'plugin_%s' % (key, ), value)

            rpctoken = RPCToken.new()
            plugin.plugin_secret = rpctoken

            plugin_path = "%s/%s" % (pjail_path, plugin.plugin_path)
            oauth_file = "%s/%s" % (plugin_path, ".oauth")

            log.debug("install_pbi: plugin_path = %s, oauth_file = %s",
                plugin_path, oauth_file)

            fd = os.open(oauth_file, os.O_WRONLY|os.O_CREAT, 0600)
            os.write(fd,"key = %s\n" % rpctoken.key)
            os.write(fd, "secret = %s\n" % rpctoken.secret)
            os.close(fd)

            try:
                log.debug("install_pbi: trying to save plugin to database")
                plugin.save()
                newplugin.append(plugin)
                log.debug("install_pbi: plugin saved to database")
                ret = True
            except Exception, e:
                log.debug("install_pbi: FAIL! %s", e)
                ret = False

        elif res and res[0] != 0:
            # pbid seems to return 255 for any kind of error
            # lets use error str output to find out what happenned
            if re.search(r'failed checksum', res[1], re.I|re.S|re.M):
                raise MiddlewareError("The file %s seems to be "
                    "corrupt, please try download it again." % (
                        pbiname,
                        )
                    )
            raise MiddlewareError(p.error)

        log.debug("install_pbi: everything went well, returning %s", ret)
        return ret

    def _get_pbi_info(self, pbifile):
        pbi = pbiname = prefix = name = version = arch = None

        p = pbi_add(flags=PBI_ADD_FLAGS_INFO, pbi=pbifile)
        out = p.info(False, -1, 'pbi information for', 'prefix', 'name', 'version', 'arch')

        if not out:
            raise MiddlewareError("This file was not identified as in PBI "
                "format, it might as well be corrupt.")

        for pair in out:
            (var, val) = pair.split('=', 1)

            var = var.lower()
            if var == 'pbi information for':
                pbiname = val
                pbi = "%s.pbi" % val

            elif var == 'prefix':
                prefix = val

            elif var == 'name':
                name = val

            elif var == 'version':
                version = val

            elif var == 'arch':
                arch = val

        return pbi, pbiname, prefix, name, version, arch

    def _get_plugin_info(self, name):
        from freenasUI.plugins.models import Plugins
        plugin = None

        qs = Plugins.objects.filter(plugin_name=name)
        if qs.count() > 0:
            plugin = qs[0]

        return  plugin

    def update_pbi(self, plugin=None):
        from freenasUI.jails.models import JailsConfiguration
        ret = False

        if not plugin:
            raise MiddlewareError("plugin is NULL")

        (c, conn) = self.__open_db(ret_conn=True)
        c.execute("SELECT plugin_jail FROM plugins_plugins WHERE id = %d" % plugin.id)
        row = c.fetchone()
        if not row:
            log.debug("update_pbi: plugins plugin not in database")
            return False
        
        jail_name = row[0]

        jail = None
        for j in Jls():
            if j.hostname == jail_name:
                jail = j
                break

        if jail is None:
            return ret

        jc = JailsConfiguration.objects.order_by("-id")[0]

        jail_root = jc.jc_path
        jail_path = "%s/%s" % (jail_root, jail_name)
        plugins_path = "%s/%s" % (jail_path, ".plugins")

        # Get new PBI settings
        newpbi, newpbiname, newprefix, newname, newversion, newarch = self._get_pbi_info(
            "/var/tmp/firmware/pbifile.pbi")

        plugin = self._get_plugin_info(newname)

        pbitemp = "/var/tmp/pbi"
        newpbifile = "%s/%s" % (plugins_path, newpbi)
        oldpbifile = "%s/%s.pbi" % (plugins_path, plugin.plugin_pbiname)

        # Rename PBI to it's actual name
        self.__system("/bin/mv /var/tmp/firmware/pbifile.pbi %s" % newpbifile)

        # Create a temporary directory to place old, new, and PBI patch files
        out = Jexec(jid=jail.jid, command="/bin/mkdir %s" % pbitemp).run()
        out = Jexec(jid=jail.jid, command="/bin/rm -f %s/*" % pbitemp).run()
        if out[0] != 0:
            raise MiddlewareError("There was a problem cleaning up the "
                "PBI temp dirctory")

        pbiname = newpbiname
        oldpbiname = "%s.pbi" % plugin.plugin_pbiname
        newpbiname = "%s.pbi" % newpbiname

        self.__umount_filesystems_within("%s%s" % (jail_path, newprefix))

        # Create a PBI from the installed version
        p = pbi_create(flags=PBI_CREATE_FLAGS_BACKUP|PBI_CREATE_FLAGS_OUTDIR,
            outdir=pbitemp, pbidir=plugin.plugin_pbiname)
        out = p.run(True, jail.jid)
        if out[0] != 0:
            raise MiddlewareError("There was a problem creating the PBI")

        # Copy the old PBI over to our temporary PBI workspace
        out = Jexec(jid=jail.jid, command="/bin/cp %s/%s %s" % (
            pbitemp, oldpbiname, "/.plugins/")).run()
        if out[0] != 0:
            raise MiddlewareError("Unable to copy old PBI file to plugins directory")

        oldpbifile = "%s/%s" % (pbitemp, oldpbiname)
        newpbifile = "%s/%s" % (pbitemp, newpbiname)

        # Copy the new PBI over to our temporary PBI workspace
        out = Jexec(jid=jail.jid, command="/bin/cp /.plugins/%s %s/" % (
            newpbiname, pbitemp)).run()
        if out[0] != 0:
            raise MiddlewareError("Unable to copy new PBI file to plugins directory")

        # Now we make the patch for the PBI upgrade
        p = pbi_makepatch(flags=PBI_MAKEPATCH_FLAGS_OUTDIR|PBI_MAKEPATCH_FLAGS_NOCHECKSIG,
            outdir=pbitemp, oldpbi=oldpbifile, newpbi=newpbifile)
        out = p.run(True, jail.jid)
        if out[0] != 0:
            raise MiddlewareError("Unable to make a PBI patch")

        pbpfile = "%s-%s_to_%s-%s.pbp" % (plugin.plugin_name,
            plugin.plugin_version, newversion, plugin.plugin_arch)

        fullpbppath = "%s/%s/%s" % (jail_path, pbitemp, pbpfile)
        if not os.access(fullpbppath, os.F_OK):
            raise MiddlewareError("Unable to create PBP file")

        # Apply the upgrade patch to upgrade the PBI to the new version
        p = pbi_patch(flags=PBI_PATCH_FLAGS_OUTDIR|PBI_PATCH_FLAGS_NOCHECKSIG,
            outdir=pbitemp, pbp="%s/%s" % (pbitemp, pbpfile))
        out = p.run(True, jail.jid)
        if out[0] != 0:
            raise MiddlewareError("Unable to patch the PBI")

        # Update the database with the new PBI version
        plugin.plugin_path = newprefix
        plugin.plugin_name = newname
        plugin.plugin_arch = newarch
        plugin.plugin_version = newversion
        plugin.plugin_pbiname = pbiname

        try:
            plugin.save()
            ret = True

        except Exception:
            ret = False

        return ret

    def delete_pbi(self, plugin):
        ret = False

        if not plugin.id:
            log.debug("delete_pbi: plugins plugin not in database")
            return False

        jail_name = plugin.plugin_jail

        jail = None
        for j in Jls():
            if j.hostname == jail_name:
                jail = j
                break

        if jail is None:
            return ret

        jail_path = j.path

        info = pbi_info(flags=PBI_INFO_FLAGS_VERBOSE)
        res = info.run(jail=True, jid=jail.jid)
        plugins = re.findall(r'^Name: (?P<name>\w+)$', res[1], re.M)

        # Plugin is not installed in the jail at all
        if res[0] == 0 and plugin.plugin_name not in plugins:
            return True

        pbi_path = os.path.join(
            jail_path,
            jail_name,
            "usr/pbi",
            "%s-%s" % (plugin.plugin_name, platform.machine()),
            )
        self.__umount_filesystems_within(pbi_path)

        p = pbi_delete(pbi=plugin.plugin_pbiname)
        res = p.run(jail=True, jid=jail.jid)
        if res and res[0] == 0:
            try:
                plugin.delete()
                ret = True

            except Exception, err:
                log.debug("delete_pbi: unable to delete pbi %s from database (%s)", plugin, err)
                ret = False

        return ret

    def get_volume_status(self, name, fs):
        status = 'UNKNOWN'
        if fs == 'ZFS':
            p1 = self.__pipeopen('zpool list -H -o health %s' % str(name))
            if p1.wait() == 0:
                status = p1.communicate()[0].strip('\n')
        elif fs == 'UFS':

            provider = self.get_label_consumer('ufs', name)
            if not provider:
                return 'UNKNOWN'
            gtype = provider.xpathEval("../../name")[0].content

            if gtype in ('MIRROR', 'STRIPE', 'RAID3'):

                search = provider.xpathEval("../config/State")
                if len(search) > 0:
                    status = search[0].content

            else:
                p1 = self.__pipeopen('mount|grep "/dev/ufs/%s"' % (name, ))
                p1.communicate()
                if p1.returncode == 0:
                    status = 'HEALTHY'
                else:
                    status = 'DEGRADED'

        if status in ('UP', 'COMPLETE', 'ONLINE'):
            status = 'HEALTHY'
        return status

    def checksum(self, path, algorithm='sha256'):
        algorithm2map = {
            'sha256' : '/sbin/sha256 -q',
        }
        hasher = self.__pipeopen('%s %s' % (algorithm2map[algorithm], path))
        sum = hasher.communicate()[0].split('\n')[0]
        return sum

    def get_disks(self):
        """
        Grab usable disks and pertinent info about them
        This accounts for:
            - all the disks the OS found
                (except the ones that are providers for multipath)
            - multipath geoms providers

        Returns:
            Dict of disks
        """
        disksd = {}

        disks = self.__get_disks()

        """
        Replace devnames by its multipath equivalent
        """
        for mp in self.multipath_all():
            for dev in mp.devices:
                if dev in disks:
                    disks.remove(dev)
            disks.append(mp.devname)

        for disk in disks:
            info = self.__pipeopen('/usr/sbin/diskinfo %s' % disk).communicate()[0].split('\t')
            if len(info) > 3:
                disksd.update({
                    disk: {
                        'devname': info[0],
                        'capacity': info[2],
                    },
                })

        return disksd

    def get_partitions(self, try_disks=True):
        disks = self.get_disks().keys()
        partitions = {}
        for disk in disks:

            listing = glob.glob('/dev/%s[a-fps]*' % disk)
            if try_disks is True and len(listing) == 0:
                listing = [disk]
            for part in list(listing):
                toremove = len([i for i in listing if i.startswith(part) and i != part]) > 0
                if toremove:
                    listing.remove(part)

            for part in listing:
                p1 = Popen(["/usr/sbin/diskinfo", part], stdin=PIPE, stdout=PIPE)
                info = p1.communicate()[0].split('\t')
                partitions.update({
                    part: {
                        'devname': info[0].replace("/dev/", ""),
                        'capacity': info[2]
                    },
                })
        return partitions

    def precheck_partition(self, dev, fstype):

        if fstype == 'UFS':
            p1 = self.__pipeopen("/sbin/fsck_ufs -p %s" % dev)
            p1.communicate()
            if p1.returncode == 0:
                return True
        elif fstype == 'NTFS':
            return True
        elif fstype == 'MSDOSFS':
            p1 = self.__pipeopen("/sbin/fsck_msdosfs -p %s" % dev)
            p1.communicate()
            if p1.returncode == 0:
                return True
        elif fstype == 'EXT2FS':
            p1 = self.__pipeopen("/sbin/fsck_ext2fs -p %s" % dev)
            p1.communicate()
            if p1.returncode == 0:
                return True

        return False

    def label_disk(self, label, dev, fstype=None):
        """
        Label the disk being manually imported
        Currently UFS, NTFS, MSDOSFS and EXT2FS are supported
        """

        if fstype == 'UFS':
            p1 = Popen(["/sbin/tunefs", "-L", label, dev], stdin=PIPE, stdout=PIPE)
        elif fstype == 'NTFS':
            p1 = Popen(["/usr/local/sbin/ntfslabel", dev, label], stdin=PIPE, stdout=PIPE)
        elif fstype == 'MSDOSFS':
            p1 = Popen(["/usr/local/bin/mlabel", "-i", dev, "::%s" % label], stdin=PIPE, stdout=PIPE)
        elif fstype == 'EXT2FS':
            p1 = Popen(["/usr/local/sbin/tune2fs", "-L", label, dev], stdin=PIPE, stdout=PIPE)
        elif fstype is None:
            p1 = Popen(["/sbin/geom", "label", "label", label, dev], stdin=PIPE, stdout=PIPE)
        else:
            return False, 'Unknown fstype %r' % fstype
        err = p1.communicate()[1]
        if p1.returncode == 0:
            return True, ''
        return False, err

    def detect_volumes(self, extra=None):
        """
        Responsible to detect existing volumes by running
        g{mirror,stripe,raid3},zpool commands

        Used by: Automatic Volume Import
        """

        volumes = []
        doc = self.__geom_confxml()
        # Detect GEOM mirror, stripe and raid3
        for geom in ('mirror', 'stripe', 'raid3'):
            search = doc.xpathEval("//class[name = '%s']/geom/config" % (geom.upper(),))
            for entry in search:
                label = entry.xpathEval('../name')[0].content
                disks = []
                for consumer in entry.xpathEval('../consumer/provider'):
                    provider = consumer.prop("ref")
                    device = doc.xpathEval("//class[name = 'DISK']//provider[@id = '%s']/name" % provider)
                    # The raid might be degraded
                    if len(device) > 0:
                        disks.append({'name': device[0].content})

                # Next thing is find out whether this is a raw block device or has GPT
                #TODO: MBR?
                search = doc.xpathEval("//class[name = 'PART']/geom[name = '%s/%s']/provider//config[type = 'freebsd-ufs']" % (geom,label))
                if len(search) > 0:
                    label = search[0].xpathEval("../name")[0].content.split('/', 1)[1]
                volumes.append({
                    'label': label,
                    'type': 'geom',
                    'group_type': geom,
                    'disks': {'vdevs': [{'disks': disks, 'name': geom}]},
                    })

        pool_name = re.compile(r'pool: (?P<name>%s).*?id: (?P<id>\d+)' % (zfs.ZPOOL_NAME_RE, ), re.I|re.M|re.S)
        p1 = self.__pipeopen("zpool import")
        res = p1.communicate()[0]

        for pool, zid in pool_name.findall(res):
            # get status part of the pool
            status = res.split('id: %s\n' % zid)[1].split('pool:')[0]
            roots = zfs.parse_status(pool, doc, 'id: %s\n%s' % (zid, status))

            if roots['data'].status != 'UNAVAIL':
                volumes.append({
                    'label': pool,
                    'type': 'zfs',
                    'id': roots.id,
                    'group_type': 'none',
                    'cache': roots['cache'].dump() if roots['cache'] else None,
                    'log': roots['logs'].dump() if roots['logs'] else None,
                    'spare': roots['spares'].dump() if roots['spares'] else None,
                    'disks': roots['data'].dump(),
                    })

        return volumes

    def zfs_import(self, name, id=None):
        if id is not None:
            imp = self.__pipeopen('zpool import -f -R /mnt %s' % id)
        else:
            imp = self.__pipeopen('zpool import -f -R /mnt %s' % name)
        stdout, stderr = imp.communicate()
        if imp.returncode == 0:
            # Reset all mountpoints in the zpool
            self.zfs_inherit_option(name, 'mountpoint', True)
            # Remember the pool cache
            self.__system("zpool set cachefile=/data/zfs/zpool.cache %s" % (name))
            # These should probably be options that are configurable from the GUI
            self.__system("zfs set aclmode=passthrough %s" % name)
            self.__system("zfs set aclinherit=passthrough %s" % name)
            return True
        else:
            log.error("Importing %s [%s] failed with: %s",
                name,
                id,
                stderr)
        return False

    def volume_detach(self, vol_name, vol_fstype):
        """Detach a volume from the system

        This either executes exports a zpool or umounts a generic volume (e.g.
        NTFS, UFS, etc).

        In the event that the volume is still in use in the OS, the end-result
        is implementation defined depending on the filesystem, and the set of
        commands used to export the filesystem.

        Finally, this method goes and cleans up the mountpoint. This is a
        sanity check to ensure that things are in synch.

        XXX: recursive unmounting / needs for recursive unmounting here might
             be a good idea.
        XXX: better feedback about files in use might be a good idea...
             someday. But probably before getting to this point. This is a
             tricky problem to fix in a way that doesn't unnecessarily suck up
             resources, but also ensures that the user is provided with
             meaningful data.
        XXX: this doesn't work with the alternate mountpoint functionality
             available in UFS volumes.

        Parameters:
            vol_name: a textual name for the volume, e.g. tank, stripe, etc.
            vol_fstype: the filesystem type for the volume; valid values are:
                        'EXT2FS', 'MSDOSFS', 'UFS', 'ZFS'.

        Raises:
            MiddlewareError: the volume could not be detached cleanly.
            MiddlewareError: the volume's mountpoint couldn't be removed.
        """

        vol_mountpath = self.__get_mountpath(vol_name, vol_fstype)
        if vol_fstype == 'ZFS':
            cmds = [ 'zpool export %s' % (vol_name, ) ]
        else:
            cmds = [ 'umount %s' % (vol_mountpath, ) ]

        for cmd in cmds:
            p1 = self.__pipeopen(cmd)
            stdout, stderr = p1.communicate()
            if p1.returncode:
                raise MiddlewareError('Failed to detach %s with "%s" (exited '
                                      'with %d): %s'
                                      % (vol_name, cmd, p1.returncode,
                                         stderr, )) 
        self.__rmdir_mountpoint(vol_mountpath)


    def __rmdir_mountpoint(self, path):
        """Remove a mountpoint directory designated by path

        This only nukes mountpoints that exist in /mnt as alternate mointpoints
        can be specified with UFS, which can take down mission critical
        subsystems.

        This purposely doesn't use shutil.rmtree to avoid removing files that
        were potentially hidden by the mount.

        Parameters:
            path: a path suffixed with /mnt that points to a mountpoint that
                  needs to be nuked.

        XXX: rewrite to work outside of /mnt and handle unmounting of
             non-critical filesystems.
        XXX: remove hardcoded reference to /mnt .

        Raises:
            MiddlewareError: the volume's mountpoint couldn't be removed.
        """

        if path.startswith('/mnt'):
            # UFS can be mounted anywhere. Don't nuke /etc, /var, etc as the
            # underlying contents might contain something of value needed for
            # the system to continue operating.
            try:
                if os.path.isdir(path):
                    os.rmdir(path)
            except OSError as ose:
                raise MiddlewareError('Failed to remove mountpoint %s: %s'
                                      % (path, str(ose), ))


    def zfs_scrub(self, name, stop=False):
        if stop:
            imp = self.__pipeopen('zpool scrub -s %s' % str(name))
        else:
            imp = self.__pipeopen('zpool scrub %s' % str(name))
        stdout, stderr = imp.communicate()
        if imp.returncode != 0:
            raise MiddlewareError('Unable to scrub %s: %s' % (name, stderr))
        return True

    def zfs_snapshot_list(self, path=None):
        fsinfo = dict()

        zfsproc = self.__pipeopen("/sbin/zfs list -t volume -o name -H")
        zvols = filter(lambda y: y != '', zfsproc.communicate()[0].split('\n'))

        if path:
            zfsproc = self.__pipeopen("/sbin/zfs list -r -t snapshot -H -S creation %s" % path)
        else:
            zfsproc = self.__pipeopen("/sbin/zfs list -t snapshot -H -S creation")
        lines = zfsproc.communicate()[0].split('\n')
        for line in lines:
            if line != '':
                list = line.split('\t')
                snapname = list[0]
                used = list[1]
                refer = list[3]
                fs, name = snapname.split('@')
                try:
                    snaplist = fsinfo[fs]
                    mostrecent = False
                except:
                    snaplist = []
                    mostrecent = True
                snaplist.insert(0,
                    zfs.Snapshot(
                        name=name,
                        filesystem=fs,
                        used=used,
                        refer=refer,
                        mostrecent=mostrecent,
                        parent_type='filesystem' if fs not in zvols else 'volume'
                    ))
                fsinfo[fs] = snaplist
        return fsinfo

    def zfs_mksnap(self, path, name, recursive):
        if recursive:
            p1 = self.__pipeopen("/sbin/zfs snapshot -r %s@%s" % (path, name))
        else:
            p1 = self.__pipeopen("/sbin/zfs snapshot %s@%s" % (path, name))
        if p1.wait() != 0:
            err = p1.communicate()[1]
            raise MiddlewareError("Snapshot could not be taken: %s" % err)
        return True

    def zfs_clonesnap(self, snapshot, dataset):
        zfsproc = self.__pipeopen('zfs clone %s %s' % (snapshot, dataset))
        retval = zfsproc.communicate()[1]
        return retval

    def rollback_zfs_snapshot(self, snapshot):
        zfsproc = self.__pipeopen('zfs rollback %s' % (snapshot))
        retval = zfsproc.communicate()[1]
        return retval

    def config_restore(self):
        os.unlink("/data/freenas-v1.db")
        save_path = os.getcwd()
        os.chdir(FREENAS_PATH)
        self.__system("/usr/local/bin/python manage.py syncdb --noinput --migrate")
        self.__system("/usr/local/bin/python manage.py createadmin")
        os.chdir(save_path)

    def config_upload(self, uploaded_file_fd):
        config_file_name = tempfile.mktemp(dir='/var/tmp/firmware')
        try:
            with open(config_file_name, 'wb') as config_file_fd:
                for chunk in uploaded_file_fd.chunks():
                    config_file_fd.write(chunk)
            conn = sqlite3.connect(config_file_name)
            try:
                cur = conn.cursor()
                cur.execute("""SELECT name FROM sqlite_master
        WHERE type='table'
        ORDER BY name;""")
            finally:
                conn.close()
        except:
            os.unlink(config_file_name)
            return False

        shutil.move(config_file_name, '/data/uploaded.db')
        # Now we must run the migrate operation in the case the db is older
        open(NEED_UPDATE_SENTINEL, 'w+').close()

        return True

    def zfs_get_options(self, name):
        data = {}
        noinherit_fields = ['quota', 'refquota', 'reservation', 'refreservation']
        zfsname = str(name)

        zfsproc = self.__pipeopen("/sbin/zfs get -H -o property,value,source all %s" % (zfsname))
        zfs_output = zfsproc.communicate()[0]
        zfs_output = zfs_output.split('\n')
        retval = {}
        for line in zfs_output:
            if line != "":
                data = line.split('\t')
                if (not data[0] in noinherit_fields) and (data[2] == 'default' or data[2].startswith('inherited')):
                    retval[data[0]] = "inherit"
                else:
                    retval[data[0]] = data[1]
        return retval

    def zfs_set_option(self, name, item, value):
        """
        Set a ZFS attribute using zfs set

        Returns:
            tuple(bool, str)
                bool -> Success?
                str -> Error message in case of error
        """
        name = str(name)
        item = str(item)
        value = str(value)
        zfsproc = self.__pipeopen('zfs set %s=%s "%s"' % (item, value, name))
        err = zfsproc.communicate()[1]
        if zfsproc.returncode == 0:
            return True, None
        return False, err

    def zfs_inherit_option(self, name, item, recursive=False):
        """
        Inherit a ZFS attribute using zfs inherit

        Returns:
            tuple(bool, str)
                bool -> Success?
                str -> Error message in case of error
        """
        name = str(name)
        item = str(item)
        if recursive:
            zfscmd = 'zfs inherit -r %s %s' % (item, name)
        else:
            zfscmd = 'zfs inherit %s %s' % (item, name)
        zfsproc = self.__pipeopen(zfscmd)
        err = zfsproc.communicate()[1]
        if zfsproc.returncode == 0:
            return True, None
        return False, err

    def zfs_dataset_release_snapshots(self, name, recursive=False):
        name = str(name)
        retval = None
        if recursive:
            zfscmd = "/sbin/zfs list -Ht snapshot -o name,freenas:state -r %s" % (name)
        else:
            zfscmd = "/sbin/zfs list -Ht snapshot -o name,freenas:state -r -d 1 %s" % (name)
        try:
            with mntlock(blocking=False) as MNTLOCK:
                zfsproc = self.__pipeopen(zfscmd)
                output = zfsproc.communicate()[0]
                if output != '':
                    snapshots_list = output.splitlines()
                for snapshot_item in filter(None, snapshots_list):
                    snapshot, state = snapshot_item.split('\t')
                    if state != '-':
                        self.zfs_inherit_option(snapshot, 'freenas:state')
        except IOError:
            retval = 'Try again later.'
        return retval

    # Reactivate replication on all snapshots
    def zfs_dataset_reset_replicated_snapshots(self, name, recursive=False):
        name = str(name)
        retval = None
        if recursive:
            zfscmd = "/sbin/zfs list -Ht snapshot -o name,freenas:state -r %s" % (name)
        else:
            zfscmd = "/sbin/zfs list -Ht snapshot -o name,freenas:state -r -d 1 %s" % (name)
        try:
            with mntlock(blocking=False):
                zfsproc = self.__pipeopen(zfscmd)
                output = zfsproc.communicate()[0]
                if output != '':
                    snapshots_list = output.splitlines()
                for snapshot_item in filter(None, snapshots_list):
                    snapshot, state = snapshot_item.split('\t')
                    if state != 'NEW':
                        self.zfs_set_option(snapshot, 'freenas:state', 'NEW')
        except IOError:
            retval = 'Try again later.'
        return retval

    def geom_disk_replace(self, volume, to_disk):
        """Replace disk in ``volume`` for ``to_disk``

        Raises:
            ValueError: Volume not found

        Returns:
            0 if the disk was replaced, > 0 otherwise
        """

        assert volume.vol_fstype == 'UFS'

        provider = self.get_label_consumer('ufs', volume.vol_name)
        if not provider:
            raise ValueError("UFS Volume %s not found" % (volume.vol_name,))
        class_name = provider.xpathEval("../../name")[0].content
        geom_name = provider.xpathEval("../name")[0].content

        if class_name == "MIRROR":
            rv = self.__system_nolog("geom mirror forget %s" % (geom_name,))
            if rv != 0:
                return rv
            p1 = self.__pipeopen("geom mirror insert %s /dev/%s" % (str(geom_name), str(to_disk),))
            stdout, stderr = p1.communicate()
            if p1.returncode != 0:
                error = ", ".join(stderr.split('\n'))
                raise MiddlewareError('Replacement failed: "%s"' % error)
            return 0

        elif class_name == "RAID3":
            numbers = provider.xpathEval("../consumer/config/Number")
            ncomponents =int( provider.xpathEval("../config/Components")[0].content)
            numbers = [int(node.content) for node in numbers]
            lacking = [x for x in xrange(ncomponents) if x not in numbers][0]
            p1 = self.__pipeopen("geom raid3 insert -n %d %s %s" % \
                                        (lacking, str(geom_name), str(to_disk),))
            stdout, stderr = p1.communicate()
            if p1.returncode != 0:
                error = ", ".join(stderr.split('\n'))
                raise MiddlewareError('Replacement failed: "%s"' % error)
            return 0

        return 1

    def iface_destroy(self, name):
        self.__system("ifconfig %s destroy" % name)

    def interface_mtu(self, iface, mtu):
        self.__system("ifconfig %s mtu %s" % (iface, mtu))

    def guess_default_interface(self):
        p1 = self.__pipeopen("netstat -nr|egrep '^default'|awk '{ print $6}'")
        iface = p1.communicate()
        if p1.returncode != 0:
            iface = None
        try:  
            iface = iface[0].strip()
        except:
            pass
        return iface

    def lagg_remove_port(self, lagg, iface):
        return self.__system_nolog("ifconfig %s -laggport %s" % (lagg, iface))

    def __init__(self):
        self.__confxml = None
        self.__camcontrol = None
        self.__diskserial = {}
        self.__twcli = {}

    def __geom_confxml(self):
        if self.__confxml is None:
            self.__confxml = libxml2.parseDoc(self.sysctl('kern.geom.confxml'))
        return self.__confxml

    def __get_twcli(self, controller):
        if controller in self.__twcli:
            return self.__twcli[controller]

        re_port = re.compile(r'^p(?P<port>\d+).*?\bu(?P<unit>\d+)\b', re.S|re.M)
        proc = self.__pipeopen("/usr/local/sbin/tw_cli /c%d show" % (controller, ))
        output = proc.communicate()[0]

        units = {}
        for port, unit in re_port.findall(output):
            units[int(unit)] = int(port)

        self.__twcli[controller] = units
        return self.__twcli[controller]

    def serial_from_device(self, devname):
        if devname in self.__diskserial:
            return self.__diskserial.get(devname)

        args = ["/dev/%s" % devname]
        camcontrol = self._camcontrol_list()
        info = camcontrol.get(devname)
        if info is not None:
            if info.get("drv") == "rr274x_3x":
                channel = info["channel"] + 1
                if channel > 16:
                    channel -= 16
                elif channel > 8:
                    channel -= 8
                args = [
                    "/dev/%s" % info["drv"],
                    "-d",
                    "hpt,%d/%d" % (info["controller"] + 1, channel)
                    ]
            elif info.get("drv").startswith("arcmsr"):
                args = [
                    "/dev/%s%d" % (info["drv"], info["controller"]),
                    "-d",
                    "areca,%d" % (info["lun"] + 1 + (info["channel"] * 8), )
                    ]
            elif info.get("drv").startswith("hpt"):
                args = [
                    "/dev/%s" % info["drv"],
                    "-d",
                    "hpt,%d/%d" % (info["controller"] + 1, info["channel"] + 1)
                    ]
            elif info.get("drv") == "ciss":
                args = [
                    "/dev/%s%d" % (info["drv"], info["controller"]),
                    "-d",
                    "cciss,%d" % (info["channel"], )
                    ]
            elif info.get("drv") == "twa":
                twcli = self.__get_twcli(info["controller"])
                args = [
                    "/dev/%s%d" % (info["drv"], info["controller"]),
                    "-d",
                    "3ware,%d" % (twcli.get(info["channel"], -1), )
                    ]

        p1 = Popen(["/usr/local/sbin/smartctl", "-i"] + args, stdout=PIPE)
        output = p1.communicate()[0]
        search = re.search(r'Serial Number:\s+(?P<serial>.+)', output, re.I)
        if search:
            serial = search.group("serial")
            self.__diskserial[devname] = serial
            return serial
        return None

    def label_to_disk(self, name):
        """
        Given a label go through the geom tree to find out the disk name
        label = a geom label or a disk partition
        """
        doc = self.__geom_confxml()

        # try to find the provider from GEOM_LABEL
        search = doc.xpathEval("//class[name = 'LABEL']//provider[name = '%s']/../consumer/provider/@ref" % name)
        if len(search) > 0:
            provider = search[0].content
        else:
            # the label does not exist, try to find it in GEOM DEV
            search = doc.xpathEval("//class[name = 'DEV']/geom[name = '%s']//provider/@ref" % name)
            if len(search) > 0:
                provider = search[0].content
            else:
                return None
        search = doc.xpathEval("//provider[@id = '%s']/../name" % provider)
        disk = search[0].content
        if search[0].parent.parent.xpathEval("./name")[0].content in ('ELI', ):
            return self.label_to_disk(disk.replace(".eli", ""))
        return disk

    def device_to_identifier(self, name):
        name = str(name)
        doc = self.__geom_confxml()

        serial = self.serial_from_device(name)
        if serial:
            return "{serial}%s" % serial

        search = doc.xpathEval("//class[name = 'PART']/..//*[name = '%s']//config[type = 'freebsd-zfs']/rawuuid" % name)
        if len(search) > 0:
            return "{uuid}%s" % search[0].content
        search = doc.xpathEval("//class[name = 'PART']/geom/..//*[name = '%s']//config[type = 'freebsd-ufs']/rawuuid" % name)
        if len(search) > 0:
            return "{uuid}%s" % search[0].content

        search = doc.xpathEval("//class[name = 'LABEL']/geom[name = '%s']/provider/name" % name)
        if len(search) > 0:
            return "{label}%s" % search[0].content

        search = doc.xpathEval("//class[name = 'DEV']/geom[name = '%s']" % name)
        if len(search) > 0:
            return "{devicename}%s" % name

        return ''

    def identifier_to_device(self, ident):

        if not ident:
            return None

        doc = self.__geom_confxml()

        search = re.search(r'\{(?P<type>.+?)\}(?P<value>.+)', ident)
        if not search:
            return None

        tp = search.group("type")
        value = search.group("value")

        if tp == 'uuid':
            search = doc.xpathEval("//class[name = 'PART']/geom//config[rawuuid = '%s']/../../name" % value)
            if len(search) > 0:
                for entry in search:
                    if not entry.content.startswith("label"):
                        return entry.content
            return None

        elif tp == 'label':
            search = doc.xpathEval("//class[name = 'LABEL']/geom//provider[name = '%s']/../name" % value)
            if len(search) > 0:
                return search[0].content
            return None

        elif tp == 'serial':
            for devname in self.__get_disks():
                serial = self.serial_from_device(devname)
                if serial == value:
                    return devname
            return None

        elif tp == 'devicename':
            search = doc.xpathEval("//class[name = 'DEV']/geom[name = '%s']" % value)
            if len(search) > 0:
                return value
            return None
        else:
            raise NotImplementedError

    def part_type_from_device(self, name, device):
        """
        Given a partition a type and a disk name (adaX)
        get the first partition that matches the type
        """
        doc = self.__geom_confxml()
        #TODO get from MBR as well?
        search = doc.xpathEval("//class[name = 'PART']/geom[name = '%s']//config[type = 'freebsd-%s']/../name" % (device, name))
        if len(search) > 0:
            return search[0].content
        else:
            return ''

    def swap_from_diskid(self, diskid):
        from freenasUI.storage.models import Disk
        disk = Disk.objects.get(id=diskid)
        return self.part_type_from_device('swap', disk.devname)

    def swap_from_identifier(self, ident):
        return self.part_type_from_device('swap', self.identifier_to_device(ident))

    def get_label_consumer(self, geom, name):
        """
        Get the label consumer of a given ``geom`` with name ``name``

        Returns:
            The provider xmlnode if found, None otherwise
        """
        doc = self.__geom_confxml()
        xpath = doc.xpathEval("//class[name = 'LABEL']//provider[name = '%s']/../consumer/provider/@ref" % "%s/%s" % (geom, name))
        if not xpath:
            return None
        providerid = xpath[0].content
        provider = doc.xpathEval("//provider[@id = '%s']" % providerid)[0]

        class_name = provider.xpathEval("../../name")[0].content

        # We've got a GPT over the softraid, not raw UFS filesystem
        # So we need to recurse one more time
        if class_name == 'PART':
            providerid = provider.xpathEval("../consumer/provider/@ref")[0].content
            newprovider = doc.xpathEval("//provider[@id = '%s']" % providerid)[0]
            class_name = newprovider.xpathEval("../../name")[0].content
            # if this PART is really backed up by softraid the hypothesis was correct
            if class_name in ('STRIPE', 'MIRROR', 'RAID3'):
                return newprovider

        return provider

    def get_disks_from_provider(self, provider):
        disks = []
        geomname = provider.xpathEval("../../name")[0].content
        if geomname in ('DISK', 'PART'):
            disks.append(provider.xpathEval("../name")[0].content)
        elif geomname in ('STRIPE', 'MIRROR', 'RAID3'):
            doc = self.__geom_confxml()
            for prov in provider.xpathEval("../consumer/provider/@ref"):
                prov2 = doc.xpathEval("//provider[@id = '%s']" % prov.content)[0]
                disks.append(prov2.xpathEval("../name")[0].content)
        else:
            #TODO log, could not get disks
            pass
        return disks

    def zpool_parse(self, name):
        doc = self.__geom_confxml()
        p1 = self.__pipeopen("zpool status %s" % name)
        res = p1.communicate()[0]
        parse = zfs.parse_status(name, doc, res)
        return parse

    def _camcontrol_list(self):
        """
        Parse camcontrol devlist -v output to gather
        controller id, channel no and driver from a device

        Returns:
            dict(devname) = dict(drv, controller, channel)
        """
        if self.__camcontrol is not None:
            return self.__camcontrol

        self.__camcontrol = {}

        """
        Hacky workaround

        It is known that at least some HPT controller have a bug in the
        camcontrol devlist output with multiple controllers, all controllers
        will be presented with the same driver with index 0
        e.g. two hpt27xx0 instead of hpt27xx0 and hpt27xx1

        What we do here is increase the controller id by its order of
        appearance in the camcontrol output
        """
        hptctlr = defaultdict(int)

        re_drv_cid = re.compile(r'.* on (?P<drv>.*?)(?P<cid>[0-9]+) bus', re.S|re.M)
        re_tgt = re.compile(r'target (?P<tgt>[0-9]+) .*?lun (?P<lun>[0-9]+) .*\((?P<dv1>[a-z]+[0-9]+),(?P<dv2>[a-z]+[0-9]+)\)', re.S|re.M)
        drv, cid, tgt, lun, dev, devtmp = (None, ) * 6

        proc = self.__pipeopen("camcontrol devlist -v")
        for line in proc.communicate()[0].splitlines():
            if not line.startswith('<'):
                reg = re_drv_cid.search(line)
                if not reg:
                    continue
                drv = reg.group("drv")
                if drv.startswith("hpt"):
                    cid = hptctlr[drv]
                    hptctlr[drv] += 1
                else:
                    cid = reg.group("cid")
            else:
                reg = re_tgt.search(line)
                if not reg:
                    continue
                tgt = reg.group("tgt")
                lun = reg.group("lun")
                dev = reg.group("dv1")
                devtmp = reg.group("dv2")
                if dev.startswith("pass"):
                    dev = devtmp
                self.__camcontrol[dev] = {
                    'drv': drv,
                    'controller': int(cid),
                    'channel': int(tgt),
                    'lun': int(lun)
                    }
        return self.__camcontrol

    def sync_disk(self, devname):
        from freenasUI.storage.models import Disk

        # Do not sync geom classes like multipath/hast/etc
        if devname.find("/") != -1:
            return

        self.__diskserial.clear()
        self.__camcontrol = None

        ident = self.device_to_identifier(devname)
        qs = Disk.objects.filter(disk_identifier=ident)
        if ident and qs.exists():
            disk = qs[0]
            disk.disk_name = devname
            disk.disk_enabled = True
        else:
            qs = Disk.objects.filter(disk_name=devname).update(
                disk_enabled=False
            )
            disk = Disk()
            disk.disk_name = devname
            disk.disk_identifier = ident
            disk.disk_enabled = True
            disk.disk_serial = self.serial_from_device(devname) or ''
        disk.save()

    def sync_disks(self):
        from freenasUI.storage.models import Disk

        disks = self.__get_disks()
        self.__diskserial.clear()
        self.__camcontrol = None

        in_disks = {}
        serials = []
        for disk in Disk.objects.all():

            dskname = self.identifier_to_device(disk.disk_identifier)
            if not dskname or dskname in in_disks:
                # If we cant translate the indentifier to a device, give up
                # If dskname has already been seen once then we are probably
                # dealing with with multipath here
                disk.delete()
                continue
            else:
                disk.disk_enabled = True
                if dskname != disk.disk_name:
                    disk.disk_name = dskname

            if disk.disk_serial:
                serials.append(disk.disk_serial)

            if dskname not in disks:
                disk.disk_enabled = False
                if disk._original_state.get("disk_enabled"):
                    disk.save()
                else:
                    #Duplicated disk entries in database
                    disk.delete()
            else:
                disk.save()
            in_disks[dskname] = disk

        for disk in disks:
            if disk not in in_disks:
                d = Disk()
                d.disk_name = disk
                d.disk_identifier = self.device_to_identifier(disk)
                d.disk_serial = self.serial_from_device(disk) or ''
                if d.disk_serial:
                    if d.disk_serial in serials:
                        #Probably dealing with multipath here, do not add another
                        continue
                    else:
                        serials.append(d.disk_serial)
                d.save()

    def sync_encrypted(self, volume=None):
        """
        This syncs the EncryptedDisk table with the current state
        of a volume
        """
        from freenasUI.storage.models import EncryptedDisk, Volume
        if volume is not None:
            volumes = [volume]
        else:
            volumes = Volume.objects.filter(vol_encrypt__gt=0)

        for vol in volumes:
            """
            Parse zpool status to get encrypted providers
            """
            zpool = self.zpool_parse(vol.vol_name)
            provs = []
            for dev in zpool.get_devs():
                if not dev.name.endswith(".eli"):
                    continue
                prov = dev.name[:-4]
                qs = EncryptedDisk.objects.filter(encrypted_provider=prov)
                if not qs.exists():
                    ed = EncryptedDisk()
                    ed.encrypted_volume = vol
                    ed.encrypted_provider = dev[:-4]
                    disk = Disk.objects.filter(disk_name=dev.disk, disk_enabled=True)
                    if disk.exists():
                        disk = disk[0]
                    else:
                        log.error("Could not find Disk entry for %s", dev.disk)
                        disk = None
                    ed.encrypted_disk = None
                    ed.save()
                provs.append(prov)
            for ed in EncryptedDisk.objects.filter(encrypted_volume=vol):
                if ed.encrypted_provider not in provs:
                    ed.delete()

    def geom_disks_dump(self, volume):
        """
        Raises:
            ValueError: UFS volume not found
        """
        provider = self.get_label_consumer('ufs', volume.vol_name)
        if not provider:
            raise ValueError("UFS Volume %s not found" % (volume,))
        class_name = provider.xpathEval("../../name")[0].content

        items = []
        uid = 1
        if class_name in ('MIRROR', 'RAID3', 'STRIPE'):
            if class_name == 'STRIPE':
                statepath = "../config/State"
                status = provider.xpathEval("../config/Status")[0].content
                ncomponents = int(re.search(r'Total=(?P<total>\d+)', status).group("total"))
            else:
                statepath = "./config/State"
                ncomponents = int(provider.xpathEval("../config/Components")[0].content)
            consumers = provider.xpathEval("../consumer")
            doc = self.__geom_confxml()
            for consumer in consumers:
                provid = consumer.xpathEval("./provider/@ref")[0].content
                status = consumer.xpathEval(statepath)[0].content
                name = doc.xpathEval("//provider[@id = '%s']/../name" % provid)[0].content
                items.append({
                    'type': 'dev',
                    'diskname': name,
                    'name': name,
                    'status': status,
                })
            for i in xrange(len(consumers), ncomponents):
                items.append({
                    'type': 'dev',
                    'name': 'UNAVAIL',
                    'status': 'UNAVAIL',
                })
        elif class_name == 'PART':
            name = provider.xpathEval("../name")[0].content
            items.append({
                'type': 'dev',
                'diskname': name,
                'name': name,
                'status': 'ONLINE',
            })
        return items

    def multipath_all(self):
        """
        Get all available gmultipath instances

        Returns:
            A list of Multipath objects
        """
        doc = self.__geom_confxml()
        return [Multipath(doc=doc, xmlnode=geom) \
                for geom in doc.xpathEval("//class[name = 'MULTIPATH']/geom")
            ]

    def multipath_create(self, name, consumers):
        """
        Create an Active/Passive GEOM_MULTIPATH provider
        with name ``name`` using ``consumers`` as the consumers for it

        Returns:
            True in case the label succeeded and False otherwise
        """
        p1 = subprocess.Popen(["/sbin/gmultipath", "label", name] + consumers, stdout=subprocess.PIPE)
        # We need to invalidate confxml cache
        self.__confxml = None
        if p1.wait() != 0:
            return False
        return True

    def multipath_next(self):
        """
        Find out the next available name for a multipath named diskX
        where X is a crescenting value starting from 1

        Returns:
            The string of the multipath name to be created
        """
        RE_NAME = re.compile(r'[a-z]+(\d+)')
        numbers = sorted([int(RE_NAME.search(mp.name).group(1)) \
                        for mp in self.multipath_all() if RE_NAME.match(mp.name)
                        ])
        if not numbers:
            numbers = [0]
        for number in xrange(1, numbers[-1]+2):
            if number not in numbers:
                break
        else:
            raise ValueError('Could not find multipaths')
        return "disk%d" % number

    def multipath_sync(self):
        """Synchronize multipath disks

        Every distinct GEOM_DISK that shares an ident (aka disk serial)
        is considered a multipath and will be handled by GEOM_MULTIPATH

        If the disk is not currently in use by some Volume or iSCSI Disk Extent
        then a gmultipath is automatically created and will be available for use
        """
        from freenasUI.storage.models import Volume, Disk

        doc = self.__geom_confxml()

        mp_disks = []
        for geom in doc.xpathEval("//class[name = 'MULTIPATH']/geom"):
            for provref in geom.xpathEval("./consumer/provider/@ref"):
                prov = doc.xpathEval("//provider[@id = '%s']" % provref.content)[0]
                class_name = prov.xpathEval("../../name")[0].content
                #For now just DISK is allowed
                if class_name != 'DISK':
                    continue
                disk = prov.xpathEval("../name")[0].content
                mp_disks.append(disk)

        reserved = [self.__find_root_dev()]

        # disks already in use count as reserved as well
        for vol in Volume.objects.all():
            reserved.extend(vol.get_disks())

        disks = []
        serials = defaultdict(list)
        RE_CD = re.compile('^cd[0-9]')
        for geom in doc.xpathEval("//class[name = 'DISK']/geom"):
            name = geom.xpathEval("./name")[0].content
            if RE_CD.match(name) or name in reserved or name in mp_disks:
                continue
            serial = self.serial_from_device(name)
            size = geom.xpathEval("./provider/mediasize")[0].content
            if not serial:
                disks.append(name)
            else:
                serials[(serial, size)].append(name)

        disks = sorted(disks)

        for disks in serials.values():
            if not len(disks) > 1:
                continue
            name = self.multipath_next()
            self.multipath_create(name, disks)

        # Grab confxml again to take new multipaths into account
        doc = self.__geom_confxml()
        mp_ids = []
        for geom in doc.xpathEval("//class[name = 'MULTIPATH']/geom"):
            _disks = []
            for provref in geom.xpathEval("./consumer/provider/@ref"):
                prov = doc.xpathEval("//provider[@id = '%s']" % provref.content)[0]
                class_name = prov.xpathEval("../../name")[0].content
                #For now just DISK is allowed
                if class_name != 'DISK':
                    continue
                disk = prov.xpathEval("../name")[0].content
                _disks.append(disk)
            qs = Disk.objects.filter(
                Q(disk_name__in=_disks)|Q(disk_multipath_member__in=_disks)
                )
            if qs.exists():
                diskobj = qs[0]
                mp_ids.append(diskobj.id)
                diskobj.disk_multipath_name = geom.xpathEval("./name")[0].content
                if diskobj.disk_name in _disks:
                    _disks.remove(diskobj.disk_name)
                if _disks:
                    diskobj.disk_multipath_member = _disks.pop()
                diskobj.save()

        Disk.objects.exclude(id__in=mp_ids).update(disk_multipath_name='', disk_multipath_member='')


    def __find_root_dev(self):
        """Find the root device.

        The original algorithm was adapted from /root/updatep*, but this
        grabs the relevant information from geom's XML facility.

        Returns:
             The root device name in string format, e.g. FreeNASp1,
             FreeNASs2, etc.

        Raises:
             AssertionError: the root device couldn't be determined.
        """

        sw_name = get_sw_name()
        doc = self.__geom_confxml()

        for pref in doc.xpathEval("//class[name = 'LABEL']/geom/provider[" \
                "starts-with(name, 'ufs/%ss')]/../consumer/provider/@ref" \
                % (sw_name, )):
            prov = doc.xpathEval("//provider[@id = '%s']" % pref.content)[0]
            pid = prov.xpathEval("../consumer/provider/@ref")[0].content
            prov = doc.xpathEval("//provider[@id = '%s']" % pid)[0]
            name = prov.xpathEval("../name")[0].content
            return name
        raise AssertionError('Root device not found (!)')


    def __get_disks(self):
        """Return a list of available storage disks.

        The list excludes all devices that cannot be reserved for storage,
        e.g. the root device, CD drives, etc.

        Returns:
            A list of available devices (ada0, da0, etc), or an empty list if
            no devices could be divined from the system.
        """

        disks = self.sysctl('kern.disks').split()
        disks.reverse()

        root_dev = self.__find_root_dev()
        if root_dev.startswith('mirror/'):
            mirror = self.gmirror_status(root_dev.split('/')[1])
            blacklist_devs = [c.get("name") for c in mirror.get("consumers")]
        else:
            blacklist_devs = [root_dev]

        device_blacklist_re = re.compile('a?cd[0-9]+')

        return filter(lambda x: not device_blacklist_re.match(x) and x not in blacklist_devs, disks)

    def retaste_disks(self):
        """
        Retaste disks for GEOM metadata

        This will not work if the device is already open

        It is useful in multipath situations, for example.
        """
        disks = self.__get_disks()
        for disk in disks:
            open("/dev/%s" % disk, 'w').close()

    def gmirror_status(self, name):
        """
        Get all available gmirror instances

        Returns:
            A dict describing the gmirror
        """

        doc = self.__geom_confxml()
        for geom in doc.xpathEval("//class[name = 'MIRROR']/geom[name = '%s']" % name):
            consumers = []
            gname = geom.xpathEval("./name")[0].content
            status = geom.xpathEval("./config/State")[0].content
            for consumer in geom.xpathEval("./consumer"):
                ref = consumer.xpathEval("./provider/@ref")[0]
                prov = doc.xpathEval("//provider[@id = '%s']" % ref.content)[0]
                name = prov.xpathEval("./name")[0].content
                status = consumer.xpathEval("./config/State")[0].content
                consumers.append({
                    'name': name,
                    'status': status,
                    })
            return {
                'name': gname,
                'status': status,
                'consumers': consumers,
            }
        return None

    def kern_module_is_loaded(self, module):
        """Determine whether or not a kernel module (or modules) is loaded.

        Parameter:
            module_name - a module to look for in kldstat -v output (.ko is
                          added automatically for you).

        Returns:
            A boolean to denote whether or not the module was found.
        """

        pipe = self.__pipeopen('/sbin/kldstat -v')

        return 0 < pipe.communicate()[0].find(module + '.ko')


    def zfs_get_version(self):
        """Get the ZFS (SPA) version reported via zfs(4).

        This allows us to better tune warning messages and provide
        conditional support for features in the GUI/CLI.

        Returns:
            An integer corresponding to the version retrieved from zfs(4) or
            0 if the module hasn't been loaded.

        Raises:
            ValueError: the ZFS version could not be parsed from sysctl(8).
        """

        if not self.kern_module_is_loaded('zfs'):
            return 0

        try:
            version = self.sysctl('vfs.zfs.version.spa', _type='INT')
        except ValueError, ve:
            raise ValueError('Could not determine ZFS version: %s'
                             % (str(ve), ))
        if 0 < version:
            return version
        raise ValueError('Invalid ZFS (SPA) version: %d' % (version, ))

    def __sysctl_error(self, libc, name):
        errloc = getattr(libc,'__error')
        errloc.restype = ctypes.POINTER(ctypes.c_int)
        error = errloc().contents.value
        if error == errno.ENOENT:
            msg = "The name is unknown."
        elif error == errno.ENOMEM:
            msg = "The length pointed to by oldlenp is too short to hold " \
                  "the requested value."
        else:
            msg = "Unknown error (%d)" % (error, )
        raise AssertionError("Sysctl by name (%s) failed: %s" % (name, msg))

    def sysctl(self, name, value=None, osize=None, _type='CHAR'):
        """Get any sysctl value using libc call

        This cut down the overhead of launching subprocesses

        XXX: reimplment with a C extension because ctypes.CDLL can be leaky and
             has a tendency to crash given the right inputs.

        Returns:
            The value of the given ``name'' sysctl

        Raises:
            AssertionError: sysctlbyname(3) returned an error
        """

        log.debug("sysctlbyname: %s", name)

        if value:
            #TODO: set sysctl
            raise NotImplementedError

        libc = ctypes.CDLL('libc.so.7')
        size = ctypes.c_size_t()

        if _type in ('CHAR', 'VOID'):
            if osize is None:
                #We need find out the size
                rv = libc.sysctlbyname(str(name), None, ctypes.byref(size), None, 0)
                if rv != 0:
                    self.__sysctl_error(libc, name)
            else:
                size.value = osize

            arg = (ctypes.c_ubyte * size.value)()
            buf = Byte(arg)

            if _type == 'VOID':
                buf.as_string = False

        else:
            buf = ctypes.c_int()
            size.value = ctypes.sizeof(buf)
            arg = ctypes.byref(buf)

        # Grab the sysctl value
        rv = libc.sysctlbyname(str(name), arg, ctypes.byref(size), None, 0)
        if rv != 0:
            self.__sysctl_error(libc, name)

        return buf.value

    def staticroute_delete(self, sr):
        """
        Delete a static route from the route table

        Raises:
            MiddlewareError in case the operation failed
        """
        import ipaddr
        netmask = ipaddr.IPNetwork(sr.sr_destination)
        masked = netmask.masked().compressed
        p1 = self.__pipeopen("/sbin/route delete %s" % masked)
        if p1.wait() != 0:
            raise MiddlewareError("Failed to remove the route %s" % sr.sr_destination)

    def mount_volume(self, volume):
        """
        Mount a volume.
        The volume must be in /etc/fstab

        Returns:
            True if volume was sucessfully mounted, False otherwise
        """
        if volume.vol_fstype == 'ZFS':
            raise NotImplementedError("No donuts for you!")

        prov = self.get_label_consumer(volume.vol_fstype.lower(),
            str(volume.vol_name))
        if not prov:
            return False

        proc = self.__pipeopen("mount /dev/%s/%s" % (
            volume.vol_fstype.lower(),
            volume.vol_name,
            ))
        if proc.wait() != 0:
            return False
        return True

    def __get_geoms_recursive(self, prvid):
        """
        Get _ALL_ geom nodes that depends on a given provider
        """
        doc = self.__geom_confxml()
        geoms = []
        for c in doc.xpathEval("//consumer/provider[@ref = '%s']" % (prvid, )):
            geom = c.parent.parent
            if geom.name != 'geom':
                continue
            geoms.append(geom)
            for prov in geom.xpathEval('./provider'):
                geoms.extend(self.__get_geoms_recursive(prov.prop('id')))

        return geoms

    def disk_get_consumers(self, devname):
        doc = self.__geom_confxml()
        geom = doc.xpathEval("//class[name = 'DISK']/geom[name = '%s']" % (
            devname,
            ))
        if geom:
            provid = geom[0].xpathEval("./provider/@id")[0].content
        else:
            raise ValueError("Unknown disk %s" % (devname, ))
        return self.__get_geoms_recursive(provid)

    def _do_disk_wipe_quick(self, devname):
        pipe = self.__pipeopen("dd if=/dev/zero of=/dev/%s bs=1m count=1" % (devname, ))
        err = pipe.communicate()[1]
        if pipe.returncode != 0:
            raise MiddlewareError(
                "Failed to wipe %s: %s" % (devname, err)
            )
        try:
            p1 = self.__pipeopen("diskinfo %s" % (devname, ))
            size = int(re.sub(r'\s+', ' ', p1.communicate()[0]).split()[2]) / (1024)
        except:
            log.error("Unable to determine size of %s", devname)
        else:
            pipe = self.__pipeopen("dd if=/dev/zero of=/dev/%s bs=1m oseek=%s" % (
                devname,
                size / 1024 - 4,
            ))
            pipe.communicate()

    def disk_wipe(self, devname, mode='quick'):
        if mode == 'quick':
            doc = self.__geom_confxml()
            parts = [node.content for node in doc.xpathEval("//class[name = 'PART']/geom[name = '%s']/provider/name" % devname)]
            """
            Wipe beginning and the end of every partition
            This should erase ZFS label and such to prevent further errors on replace
            """
            for part in parts:
                self._do_disk_wipe_quick(part)
            self.__gpt_unlabeldisk(devname)
            self._do_disk_wipe_quick(devname)

        elif mode in ('full', 'fullrandom'):
            libc = ctypes.cdll.LoadLibrary("libc.so.7")
            omask = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
            mask = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
            pmask = ctypes.pointer(mask)
            pomask = ctypes.pointer(omask)
            libc.sigprocmask(signal.SIGQUIT, pmask, pomask)

            self.__gpt_unlabeldisk(devname)
            stderr = open('/var/tmp/disk_wipe_%s.progress' % (devname, ), 'w+')
            stderr.flush()
            pipe = subprocess.Popen([
                "dd",
                "if=/dev/zero" if mode == 'full' else "if=/dev/random",
                "of=/dev/%s" % (devname, ),
                "bs=1m",
                ],
                stdout=subprocess.PIPE,
                stderr=stderr,
                )
            with open('/var/tmp/disk_wipe_%s.pid' % (devname, ), 'w') as f:
                f.write(str(pipe.pid))
            pipe.communicate()
            stderr.seek(0)
            err = stderr.read()
            print err
            libc.sigprocmask(signal.SIGQUIT, pomask, None)
            if pipe.returncode != 0 and err.find("end of device") == -1:
                raise MiddlewareError(
                    "Failed to wipe %s: %s" % (devname, err)
                    )
        else:
            raise ValueError("Unknown mode %s" % (mode, ))


def usage():
    usage_str = """usage: %s action command
    Action is one of:
        start: start a command
        stop: stop a command
        restart: restart a command
        reload: reload a command (try reload; if unsuccessful do restart)
        change: notify change for a command (try self.reload; if unsuccessful do start)""" \
        % (os.path.basename(sys.argv[0]), )
    sys.exit(usage_str)

# When running as standard-alone script
if __name__ == '__main__':
    if len(sys.argv) < 2:
        usage()
    else:
        n = notifier()
        f = getattr(n, sys.argv[1], None)
        if f is None:
            sys.stderr.write("Unknown action: %s\n" % sys.argv[1])
            usage()
        print f(*sys.argv[2:])
