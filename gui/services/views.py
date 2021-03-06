#+
# Copyright 2010 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################
import logging

from django.http import HttpResponse
from django.shortcuts import render
from django.utils import simplejson
from django.utils.translation import ugettext as _

from freenasUI.middleware.notifier import notifier
from freenasUI.services import models
from freenasUI.services.directoryservice import DirectoryService

log = logging.getLogger("services.views")


def index(request):
    return render(request, 'services/index.html', {
        'toggleCore': request.GET.get('toggleCore'),
    })


def core(request):

    try:
        directoryservice = DirectoryService.objects.order_by("-id")[0]
    except IndexError:
        try:
            directoryservice = DirectoryService.objects.create()
        except:
            directoryservice = None

    try:
        afp = models.AFP.objects.order_by("-id")[0]
    except IndexError:
        afp = models.AFP.objects.create()

    try:
        cifs = models.CIFS.objects.order_by("-id")[0]
    except IndexError:
        cifs = models.CIFS.objects.create()

    try:
        dynamicdns = models.DynamicDNS.objects.order_by("-id")[0]
    except IndexError:
        dynamicdns = models.DynamicDNS.objects.create()

    try:
        nfs = models.NFS.objects.order_by("-id")[0]
    except IndexError:
        nfs = models.NFS.objects.create()

    try:
        ftp = models.FTP.objects.order_by("-id")[0]
    except IndexError:
        ftp = models.FTP.objects.create()

    try:
        tftp = models.TFTP.objects.order_by("-id")[0]
    except IndexError:
        tftp = models.TFTP.objects.create()

    try:
        rsyncd = models.Rsyncd.objects.order_by("-id")[0]
    except IndexError:
        rsyncd = models.Rsyncd.objects.create()

    try:
        smart = models.SMART.objects.order_by("-id")[0]
    except IndexError:
        smart = models.SMART.objects.create()

    try:
        snmp = models.SNMP.objects.order_by("-id")[0]
    except IndexError:
        snmp = models.SNMP.objects.create()

    try:
        ssh = models.SSH.objects.order_by("-id")[0]
    except IndexError:
        ssh = models.SSH.objects.create()

    try:
        ups = models.UPS.objects.order_by("-id")[0]
    except IndexError:
        ups = models.UPS.objects.create()

    srv = models.services.objects.all()
    return render(request, 'services/core.html', {
        'srv': srv,
        'cifs': cifs,
        'afp': afp,
        'nfs': nfs,
        'rsyncd': rsyncd,
        'dynamicdns': dynamicdns,
        'snmp': snmp,
        'ups': ups,
        'ftp': ftp,
        'tftp': tftp,
        'smart': smart,
        'ssh': ssh,
        'directoryservice': directoryservice,
    })


def iscsi(request):
    gconfid = models.iSCSITargetGlobalConfiguration.objects.all().order_by(
        "-id")[0].id
    return render(request, 'services/iscsi.html', {
        'focus_tab': request.GET.get('tab', ''),
        'gconfid': gconfid,
    })


def servicesToggleView(request, formname):
    form2namemap = {
        'cifs_toggle': 'cifs',
        'afp_toggle': 'afp',
        'nfs_toggle': 'nfs',
        'iscsitarget_toggle': 'iscsitarget',
        'dynamicdns_toggle': 'dynamicdns',
        'snmp_toggle': 'snmp',
        'httpd_toggle': 'httpd',
        'ftp_toggle': 'ftp',
        'tftp_toggle': 'tftp',
        'ssh_toggle': 'ssh',
        'ldap_toggle': 'ldap',
        'rsync_toggle': 'rsync',
        'smartd_toggle': 'smartd',
        'ups_toggle': 'ups',
        'plugins_toggle': 'plugins',
        'directoryservice_toggle': 'directoryservice',
    }
    changing_service = form2namemap[formname]
    if changing_service == "":
        raise "Unknown service - Invalid request?"

    enabled_svcs = []
    disabled_svcs = []
    _notifier = notifier()

    svc_entry = models.services.objects.get(srv_service=changing_service)
    if svc_entry.srv_enable:
        svc_entry.srv_enable = False
    else:
        svc_entry.srv_enable = True

    if changing_service != 'directoryservice':
        svc_entry.save()

    directory_services = ['activedirectory', 'ldap', 'nt4', 'nis']
    if changing_service == "directoryservice":
        directoryservice = DirectoryService.objects.order_by("-id")[0]
        for ds in directory_services:
            if ds != directoryservice.svc:
                method = getattr(_notifier, "_started_%s" % ds)
                started = method()
                if started:
                    _notifier.stop(ds)

        if svc_entry.srv_enable:
            svc_entry.save()
            started = _notifier.start(directoryservice.svc)
            if models.services.objects.get(srv_service='cifs').srv_enable:
                enabled_svcs.append('cifs')
        else:
            started = _notifier.stop(directoryservice.svc)
            svc_entry.save()
            if not models.services.objects.get(srv_service='cifs').srv_enable:
                disabled_svcs.append('cifs')

    if changing_service != 'directoryservice':
        """
        Using rc.d restart verb and depend on rc_var service_enable
        does not see the best way to handle service start/stop process

        For now lets handle it properly just for ssh and snmp that seems
        to be the most affected for randomly not starting
        """
        if changing_service in ('snmp', 'ssh'):
            if svc_entry.srv_enable:
                started = _notifier.start(changing_service)
            else:
                started = _notifier.stop(changing_service)
        else:
            started = _notifier.restart(changing_service)

    error = False
    message = False
    if started is True:
        status = 'on'
        if not svc_entry.srv_enable:
            error = True
            message = _("The service could not be stopped.")
            svc_entry.srv_enable = True
            svc_entry.save()

    elif started is False:
        status = 'off'
        if svc_entry.srv_enable:
            error = True
            message = _("The service could not be started.")
            svc_entry.srv_enable = False
            svc_entry.save()
            if changing_service in ('ups', 'directoryservice'):
                if changing_service == 'directoryservice':
                    _notifier.stop(directoryservice.svc)
                else:
                    _notifier.stop(changing_service)
    else:
        if svc_entry.srv_enable:
            status = 'on'
        else:
            status = 'off'

    data = {
        'service': changing_service,
        'status': status,
        'error': error,
        'message': message,
        'enabled_svcs': enabled_svcs,
        'disabled_svcs': disabled_svcs,
    }

    return HttpResponse(simplejson.dumps(data), mimetype="application/json")


def enable(request, svc):
    return render(request, "services/enable.html", {
        'svc': svc,
    })
