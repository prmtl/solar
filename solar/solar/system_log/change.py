#    Copyright 2015 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import dictdiffer
import networkx as nx

from solar.core.log import log
from solar.core import signals
from solar.core import resource
from solar import utils
from solar.interfaces.db import get_db
from solar.system_log import data
from solar.orchestration import graph
from solar.events import api as evapi
from solar.interfaces import orm
from .consts import CHANGES
from solar.core.resource.resource import RESOURCE_STATE
from solar.errors import CannotFindID

db = get_db()


def guess_action(from_, to):
    # NOTE(dshulyak) imo the way to solve this - is dsl for orchestration,
    # something where this action will be excplicitly specified
    if not from_:
        return CHANGES.run.name
    elif not to:
        return CHANGES.remove.name
    else:
        return CHANGES.update.name


def create_diff(staged, commited):
    return list(dictdiffer.diff(commited, staged))


def create_logitem(resource, action, diffed, connections_diffed,
                   base_path=None):
    return data.LogItem(
                utils.generate_uuid(),
                resource,
                action,
                diffed,
                connections_diffed,
                base_path=base_path)


def create_sorted_diff(staged, commited):
    staged.sort()
    commited.sort()
    return create_diff(staged, commited)



def stage_changes():
    log = data.SL()
    log.clean()

    for resouce_obj in resource.load_all():
        commited = resouce_obj.load_commited()
        base_path = resouce_obj.base_path
        if resouce_obj.to_be_removed():
            resource_args = {}
            resource_connections = []
        else:
            resource_args = resouce_obj.args
            resource_connections = resouce_obj.connections

        if commited.state == RESOURCE_STATE.removed.name:
            commited_args = {}
            commited_connections = []
        else:
            commited_args = commited.inputs
            commited_connections = commited.connections

        inputs_diff = create_diff(resource_args, commited_args)
        connections_diff = create_sorted_diff(
            resource_connections, commited_connections)

        # if new connection created it will be reflected in inputs
        # but using inputs to reverse connections is not possible
        if inputs_diff:
            log_item = create_logitem(
                resouce_obj.name,
                guess_action(commited_args, resource_args),
                inputs_diff,
                connections_diff,
                base_path=base_path)
            log.append(log_item)
    return log


def send_to_orchestration():
    dg = nx.MultiDiGraph()
    events = {}
    changed_nodes = []

    for logitem in data.SL():
        events[logitem.res] = evapi.all_events(logitem.res)
        changed_nodes.append(logitem.res)

        state_change = evapi.StateChange(logitem.res, logitem.action)
        state_change.insert(changed_nodes, dg)

    evapi.build_edges(dg, events)

    # what `name` should be?
    dg.graph['name'] = 'system_log'
    return graph.create_plan_from_graph(dg)


def parameters(res, action, data):
    return {'args': [res, action],
            'type': 'solar_resource',
            # unique identifier for a node should be passed
            'target': data.get('ip')}


def check_uids_present(log, uids):
    not_valid = []
    for uid in uids:
        if log.get(uid) is None:
            not_valid.append(uid)
    if not_valid:
        raise CannotFindID('UIDS: {} not in history.'.format(not_valid))


def revert_uids(uids):
    """
    :param uids: iterable not generator
    """
    history = data.CL()
    check_uids_present(history, uids)

    for uid in uids:
        item = history.get(uid)

        if item.action == CHANGES.update.name:
            _revert_update(item)
        elif item.action == CHANGES.remove.name:
            _revert_remove(item)
        elif item.action == CHANGES.run.name:
            _revert_run(item)
        else:
            log.debug('Action %s for resource %s is a side'
                      ' effect of another action', item.action, item.res)


def _revert_remove(logitem):
    """Resource should be created with all previous connections
    """
    commited = orm.DBCommitedState.load(logitem.res)
    args = dictdiffer.revert(logitem.diff, commited.inputs)
    connections = dictdiffer.revert(logitem.signals_diff, sorted(commited.connections))
    resource.Resource(logitem.res, logitem.base_path, args=args, tags=commited.tags)
    for emitter, emitter_input, receiver, receiver_input in connections:
        emmiter_obj = resource.load(emitter)
        receiver_obj = resource.load(receiver)
        signals.connect(emmiter_obj, receiver_obj, {emitter_input: receiver_input})


def _update_inputs_connections(res_obj, args, old_connections, new_connections):
    res_obj.update(args)


    removed = []
    for item in old_connections:
        if item not in new_connections:
            removed.append(item)

    added = []
    for item in new_connections:
        if item not in old_connections:
            added.append(item)

    for emitter, _, receiver, _ in removed:
        emmiter_obj = resource.load(emitter)
        receiver_obj = resource.load(receiver)
        signals.disconnect(emmiter_obj, receiver_obj)


    for emitter, emitter_input, receiver, receiver_input in added:
        emmiter_obj = resource.load(emitter)
        receiver_obj = resource.load(receiver)
        signals.connect(emmiter_obj, receiver_obj, {emitter_input: receiver_input})


def _revert_update(logitem):
    """Revert of update should update inputs and connections
    """
    res_obj = resource.load(logitem.res)
    commited = res_obj.load_commited()

    args_to_update = dictdiffer.revert(logitem.diff, commited.inputs)
    connections = dictdiffer.revert(logitem.signals_diff, sorted(commited.connections))

    _update_inputs_connections(
        res_obj, args_to_update, commited.connections, connections)


def _revert_run(logitem):
    res_obj = resource.load(logitem.res)
    res_obj.remove()


def revert(uid):
    return revert_uids([uid])


def _discard_remove(item):
    resource_obj = resource.load(item.res)
    resource_obj.set_created()


def _discard_update(item):
    resource_obj = resource.load(item.res)
    old_connections = resource_obj.connections
    new_connections = dictdiffer.revert(item.signals_diff, sorted(old_connections))
    args = dictdiffer.revert(item.diff, resource_obj.args)
    _update_inputs_connections(
        resource_obj, args, old_connections, new_connections)

def _discard_run(item):
    resource.load(item.res).remove(force=True)


def discard_uids(uids):
    staged_log = data.SL()
    check_uids_present(staged_log, uids)
    for uid in uids:
        item = staged_log.get(uid)
        if item.action == CHANGES.update.name:
            _discard_update(item)
        elif item.action == CHANGES.remove.name:
            _discard_remove(item)
        elif item.action == CHANGES.run.name:
            _discard_run(item)
        else:
            log.debug('Action %s for resource %s is a side'
                      ' effect of another action', item.action, item.res)
        staged_log.pop(uid)


def discard_uid(uid):
    return discard_uids([uid])

def discard_all():
    staged_log = data.SL()
    return discard_uids([l.uid for l in staged_log])


def commit_all():
    """Helper mainly for ease of testing
    """
    from .operations import move_to_commited
    for item in data.SL():
        move_to_commited(item.log_action)
