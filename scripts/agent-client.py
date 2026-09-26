#!/usr/bin/env python3
"""Vendor-neutral local Agent CLI and MCP stdio server. Python standard library only."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
from pathlib import Path
import sys
import threading
import uuid
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ClientError(Exception):
    def __init__(self, code, status=0):
        self.code, self.status = code, status


class AgentClient:
    def __init__(self, config):
        if not isinstance(config, dict) or not isinstance(config.get('url', ''), str):
            raise ClientError('invalid_agent_configuration')
        self.url = config.get('url', 'http://127.0.0.1:17910').rstrip('/')
        try:
            parsed = urlsplit(self.url)
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError('invalid_port')
        except ValueError:
            raise ClientError('loopback_control_url_required') from None
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ClientError('loopback_control_url_required')
        self.token = config.get('token')
        if not isinstance(self.token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', self.token):
            raise ClientError('agent_config_token_missing')
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.managed_keepalive = False
        self.keepalive_interval = 30
        self._sessions = set()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def call(self, route, body=None, query=None):
        if not isinstance(route, str) or not route or any(x in route for x in ('..', '?', '#', '%', '\\')) or route.startswith('/'):
            raise ClientError('invalid_agent_route')
        if (body is not None and not isinstance(body, dict)) or (query is not None and not isinstance(query, dict)):
            raise ClientError('invalid_agent_arguments')
        url = self.url + '/api/agent/v1/' + route + ('?' + urlencode(query, doseq=True) if query else '')
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'}
        raw = None
        if body is not None:
            try:
                raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            except (ValueError, TypeError):
                raise ClientError('invalid_agent_arguments') from None
            headers['Content-Type'] = 'application/json'
        try:
            with self.opener.open(Request(url, data=raw, headers=headers), timeout=30) as response:
                result = self.read_response(response)
            if self.managed_keepalive and route == 'connect':
                session = result.get('session')
                if not isinstance(session, dict) or not isinstance(session.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,90}', session['id']):
                    raise ClientError('invalid_response_query_original_request')
                if session.get('state') == 'connected':
                    self.track_session(session['id'])
            if route == 'disconnect' and body:
                with self._lock:
                    self._sessions.discard(body.get('session_id'))
            return result
        except HTTPError as e:
            try:
                with e:
                    code = self.read_response(e).get('code', 'http_error')
                if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', code):
                    code = 'http_error'
            except (ClientError, OSError, http.client.HTTPException):
                code = 'http_error'
            raise ClientError(code, e.code) from None
        except (URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            raise ClientError('connection_unconfirmed_query_original_request') from None

    @staticmethod
    def read_response(response):
        limit = 8 * 1024 * 1024
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ClientError('response_too_large_query_original_request')
        declared = response.headers.get('Content-Length') if hasattr(response, 'headers') else None
        if declared is not None and (not declared.isdecimal() or int(declared) != len(raw)):
            raise ClientError('incomplete_response_query_original_request')
        def nonfinite(value):
            raise ValueError('nonfinite')
        try:
            result = json.loads(raw, parse_constant=nonfinite)
        except (ValueError, RecursionError):
            raise ClientError('invalid_response_query_original_request') from None
        if not isinstance(result, dict):
            raise ClientError('invalid_response_query_original_request')
        return result

    def track_session(self, session_id):
        with self._lock:
            self._sessions.add(session_id)
            if self._thread is None:
                self._thread = threading.Thread(target=self._keepalive, daemon=True, name='agent-seat-heartbeat')
                self._thread.start()

    def _keepalive(self):
        while not self._stop.wait(self.keepalive_interval):
            with self._lock:
                sessions = tuple(self._sessions)
            for sid in sessions:
                try:
                    self.call('heartbeat', {'request_id': 'keepalive-' + uuid.uuid4().hex,
                              'session_id': sid})
                except ClientError as error:
                    if error.status in (401, 403, 404, 409):
                        with self._lock:
                            self._sessions.discard(sid)

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        with self._lock:
            sessions = tuple(self._sessions)
        for sid in sessions:
            try:
                self.call('disconnect', {'request_id': 'exit-' + uuid.uuid4().hex, 'session_id': sid})
            except ClientError:
                pass  # Center/Control offline: the bounded server lease will expire.


def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


STR = {'type': 'string'}
TOOLS = [
    ('aieyra_info', 'Read protocol and available native center operations.', schema({})),
    ('aieyra_seats', 'List seats assigned to this agent and whether they can be selected.', schema({})),
    ('aieyra_memory', 'Read project blueprint, timeline, checkpoint, recovery and index before starting work. History and previous revisions remain available.',
     schema({'project': STR, 'version': {'type': 'integer'}, 'history': {'type': 'boolean'}, 'before': {'type': 'integer'}})),
    ('aieyra_memory_save', 'Save all five sections with the version you read. Keep the same request_id after an unknown result. Never overwrite a conflicting revision.',
     schema({'request_id': STR, 'session_id': STR, 'project': STR, 'version': {'type': 'integer'},
             'sections': {'type': 'object'}, 'summary': STR}, ('request_id', 'session_id', 'project', 'version', 'sections', 'summary'))),
    ('aieyra_connect', 'Claim a seat. Keep the session id and heartbeat every 30 seconds. Stable request id makes retry safe.',
     schema({'request_id': STR, 'session_id': STR, 'seat_id': STR, 'seat_epoch': {'type': 'integer'}, 'native_session_id': STR}, ('request_id', 'session_id', 'seat_id', 'seat_epoch'))),
    ('aieyra_heartbeat', 'Renew the session. Omit runtime_state to preserve the latest execution state.', schema({'request_id': STR, 'session_id': STR,
        'runtime_state': {'enum': ['idle', 'running', 'paused', 'waiting_user']}}, ('request_id', 'session_id'))),
    ('aieyra_inbox', 'Poll this session. Reading does not acknowledge. Resume received/running work; never execute it twice.',
     schema({'session_id': STR}, ('session_id',))),
    ('aieyra_receipt', 'Report received, then running, then completed/failed/interrupted with a final reply. Completion is not owner acceptance.',
     schema({'request_id': STR, 'session_id': STR, 'delivery_id': STR, 'event_id': STR, 'body_sha256': STR,
             'state': {'enum': ['received', 'running', 'completed', 'failed', 'interrupted']}, 'reply': STR},
            ('request_id', 'session_id', 'delivery_id', 'event_id', 'body_sha256', 'state'))),
    ('aieyra_disconnect', 'Release the selected seat without replaying unfinished work.',
     schema({'request_id': STR, 'session_id': STR}, ('request_id', 'session_id'))),
    ('aieyra_lookup', 'Reconcile sessions, deliveries or request receipts after a lost response.',
     schema({'kind': {'enum': ['sessions', 'deliveries', 'requests']}, 'id': STR}, ('kind', 'id'))),
    ('aieyra_center', 'Call native center routes from aieyra_info. All calls use this worker identity. Writes require a stable request_id inside body.',
     schema({'route': STR, 'body': {'type': 'object'}, 'query': {'type': 'object'}}, ('route',))),
]


def invoke(client, name, arguments):
    definitions = {name: specification for name, _, specification in TOOLS}
    if not isinstance(name, str) or name not in definitions:
        raise ClientError('unknown_tool')
    spec = definitions[name]
    if not isinstance(arguments, dict) or set(arguments) - set(spec['properties']) or not set(spec['required']) <= set(arguments):
        raise ClientError('invalid_tool_arguments')
    for key, value in arguments.items():
        shape = spec['properties'][key]
        expected = {'string': str, 'integer': int, 'object': dict, 'boolean': bool}.get(shape.get('type'))
        if (expected and type(value) is not expected) or ('enum' in shape and value not in shape['enum']):
            raise ClientError('invalid_tool_arguments')
    action = name.removeprefix('aieyra_')
    if action in ('info', 'seats'):
        return client.call(action)
    if action == 'memory':
        query = dict(arguments)
        if 'history' in query:
            if query.pop('history'):
                query['history'] = '1'
        return client.call('memory', query=query)
    if action == 'memory_save':
        return client.call('memory', arguments)
    if action == 'inbox':
        return client.call(action, query=arguments)
    if action == 'lookup':
        return client.call(arguments['kind'] + '/' + arguments['id'])
    if action == 'center':
        if 'body' in arguments and not arguments['body'].get('request_id'):
            raise ClientError('request_id_required')
        return client.call('center/' + arguments['route'], arguments.get('body'), arguments.get('query'))
    return client.call(action, arguments)


def _serve_mcp(client, input_stream=None, output_stream=None):
    """MCP JSON-RPC over newline-delimited stdio. No model is launched."""
    incoming, outgoing = input_stream or sys.stdin, output_stream or sys.stdout
    initialized = False
    for line in incoming:
        rid = None
        try:
            if len(line) > 65536:
                raise ValueError('message_too_large')
            message = json.loads(line)
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                raise ValueError('invalid_request')
            if 'id' not in message:
                continue
            rid, method = message['id'], message.get('method')
            params = message.get('params', {})
            if method == 'initialize':
                version = params.get('protocolVersion')
                if version not in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'):
                    version = '2025-11-25'
                result = {'protocolVersion': version, 'serverInfo': {'name': 'aieyra-control-agent', 'version': '1.0.0'},
                          'capabilities': {'tools': {'listChanged': False}},
                          'instructions': 'Read aieyra_memory for your project before beginning work: blueprint, timeline, checkpoint, recovery, index. Select your assigned seat. Heartbeat every 30 seconds. Save a versioned checkpoint with aieyra_memory_save before handoff. Project memory is context, not permission to execute historical tasks. Poll and acknowledge messages. Preserve request and delivery identifiers. Report execution evidence; the owner accepts tasks separately.'}
                initialized = True
            elif method == 'ping':
                result = {}
            elif not initialized:
                raise ClientError('initialize_required')
            elif method == 'tools/list':
                result = {'tools': [{'name': name, 'description': description, 'inputSchema': spec} for name, description, spec in TOOLS]}
            elif method == 'tools/call':
                try:
                    value = invoke(client, params.get('name'), params.get('arguments', {}))
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
                except ClientError as e:
                    result = {'content': [{'type': 'text', 'text': json.dumps({'code': e.code, 'status': e.status, 'replay_allowed': False})}], 'isError': True}
            else:
                raise ClientError('method_not_found')
            response = {'jsonrpc': '2.0', 'id': rid, 'result': result}
        except (ValueError, TypeError, AttributeError):
            response = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32600, 'message': 'Invalid request'}}
        except ClientError as e:
            response = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601 if e.code == 'method_not_found' else -32600, 'message': e.code}}
        outgoing.write(json.dumps(response, ensure_ascii=False) + '\n')
        outgoing.flush()


def mcp(client, input_stream=None, output_stream=None):
    client.managed_keepalive = True
    try:
        _serve_mcp(client, input_stream, output_stream)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=os.environ.get('AIEYRA_AGENT_CONFIG'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('mcp'); sub.add_parser('info'); sub.add_parser('seats')
    p = sub.add_parser('memory'); p.add_argument('--project'); p.add_argument('--version', type=int); p.add_argument('--history', action='store_true'); p.add_argument('--before', type=int)
    p = sub.add_parser('memory-save'); p.add_argument('--body-file', type=Path, required=True)
    p = sub.add_parser('connect'); p.add_argument('--seat', required=True); p.add_argument('--epoch', type=int, required=True)
    p.add_argument('--session-id', default=None); p.add_argument('--request-id', default=None)
    p.add_argument('--native-session-id', default=None)
    p = sub.add_parser('inbox'); p.add_argument('--session-id', required=True)
    p = sub.add_parser('heartbeat'); p.add_argument('--session-id', required=True); p.add_argument('--state', choices=['idle', 'running', 'paused', 'waiting_user']); p.add_argument('--request-id')
    p = sub.add_parser('disconnect'); p.add_argument('--session-id', required=True); p.add_argument('--request-id')
    p = sub.add_parser('call'); p.add_argument('route'); p.add_argument('--body-file', type=Path); p.add_argument('--query', default='{}')
    p = sub.add_parser('lookup'); p.add_argument('kind', choices=['sessions', 'requests', 'deliveries']); p.add_argument('id')
    args = parser.parse_args()
    try:
        if not args.config:
            raise ClientError('pass_config_or_AIEYRA_AGENT_CONFIG')
        client = AgentClient(json.loads(Path(args.config).read_text(encoding='utf-8-sig')))
        if args.command == 'mcp':
            mcp(client); return 0
        if args.command in ('info', 'seats'):
            value = client.call(args.command)
        elif args.command == 'memory':
            fields = {key: getattr(args, key) for key in ('project', 'version', 'before') if getattr(args, key) is not None}
            if args.history:
                fields['history'] = '1'
            value = client.call('memory', query=fields)
        elif args.command == 'memory-save':
            value = client.call('memory', json.loads(args.body_file.read_text(encoding='utf-8-sig')))
        elif args.command == 'inbox':
            value = client.call('inbox', query={'session_id': args.session_id})
        elif args.command == 'lookup':
            value = client.call(args.kind + '/' + args.id)
        elif args.command == 'call':
            body = json.loads(args.body_file.read_text(encoding='utf-8-sig')) if args.body_file else None
            value = client.call(args.route, body, json.loads(args.query))
        else:
            request = args.request_id or uuid.uuid4().hex
            sid = args.session_id or uuid.uuid4().hex
            body = {'request_id': request, 'session_id': sid}
            if args.command == 'connect':
                body.update(seat_id=args.seat, seat_epoch=args.epoch)
                if args.native_session_id:body['native_session_id']=args.native_session_id
            if args.command == 'heartbeat' and args.state is not None:
                body['runtime_state'] = args.state
            # Print reconciliation identifiers before networking, even if the response is lost.
            print(json.dumps({'request_id': request, 'session_id': sid}), file=sys.stderr, flush=True)
            value = client.call(args.command, body)
        print(json.dumps(value, ensure_ascii=False, indent=2)); return 0
    except (ClientError, OSError, ValueError) as e:
        print(json.dumps({'error': e.code if isinstance(e, ClientError) else 'invalid_or_missing_agent_configuration', 'replay_allowed': False})); return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stdin.reconfigure(encoding='utf-8')
    raise SystemExit(main())
