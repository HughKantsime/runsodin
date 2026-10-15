"""Minimal local SMTP peer (RFC 5321 subset) bound to 127.0.0.1 on an ephemeral port.

Used by cert-E rep tests so report delivery is proven on the SMTP wire
instead of through a substituted mail function. Supports EHLO/HELO, MAIL,
RCPT (with configurable 550 refusals), DATA, RSET, NOOP, QUIT.
"""
import socketserver
import threading


class SmtpPeer:
    def __init__(self, refuse=()):
        self.refuse = {r.lower() for r in refuse}
        self.messages = []          # dicts: mail_from, rcpt_to, data (bytes)
        self.commands = []
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        peer = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, line):
                self.wfile.write((line + '\r\n').encode())
                self.wfile.flush()

            def handle(self):
                self.reply('220 cert-peer.localhost ESMTP ready')
                mail_from, rcpts = None, []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode('utf-8', 'replace').rstrip('\r\n')
                    verb = line.split(' ', 1)[0].upper()
                    with peer._lock:
                        peer.commands.append(verb)
                    if verb in ('EHLO', 'HELO'):
                        self.reply('250 cert-peer.localhost')
                    elif verb == 'MAIL':
                        mail_from, rcpts = line.split(':', 1)[1].strip().strip('<>').split('>')[0], []
                        self.reply('250 OK')
                    elif verb == 'RCPT':
                        address = line.split(':', 1)[1].strip().strip('<>').split('>')[0]
                        if address.lower() in peer.refuse:
                            self.reply('550 Synthetic mailbox unavailable')
                        else:
                            rcpts.append(address)
                            self.reply('250 OK')
                    elif verb == 'DATA':
                        if not rcpts:
                            self.reply('503 No valid recipients')
                            continue
                        self.reply('354 End data with <CR><LF>.<CR><LF>')
                        chunks = []
                        while True:
                            data_line = self.rfile.readline()
                            if not data_line or data_line in (b'.\r\n', b'.\n'):
                                break
                            if data_line.startswith(b'..'):
                                data_line = data_line[1:]
                            chunks.append(data_line)
                        with peer._arrived:
                            peer.messages.append({'mail_from': mail_from, 'rcpt_to': list(rcpts), 'data': b''.join(chunks)})
                            peer._arrived.notify_all()
                        self.reply('250 OK queued')
                        mail_from, rcpts = None, []
                    elif verb in ('RSET', 'NOOP'):
                        mail_from, rcpts = None, []
                        self.reply('250 OK')
                    elif verb == 'QUIT':
                        self.reply('221 Bye')
                        return
                    else:
                        self.reply('502 Command not implemented')

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def wait_for(self, count, timeout=10):
        with self._arrived:
            assert self._arrived.wait_for(lambda: len(self.messages) >= count, timeout=timeout), \
                f'SMTP peer received {len(self.messages)} of {count} messages'
            return list(self.messages)

    def config(self, **extra):
        return {'enabled': True, 'host': '127.0.0.1', 'port': self.port, 'use_tls': False,
                'from_address': 'odin-reports@cert.localhost', **extra}


def test_peer_speaks_smtp_to_stdlib_client():
    import smtplib
    with SmtpPeer(refuse=['refused@example.test']) as peer:
        with smtplib.SMTP('127.0.0.1', peer.port, timeout=10) as client:
            refused = client.sendmail('a@example.test', ['b@example.test', 'refused@example.test'], b'Subject: x\r\n\r\nbody\r\n')
        assert set(refused) == {'refused@example.test'}
        message = peer.wait_for(1)[0]
        assert message['rcpt_to'] == ['b@example.test'] and b'body' in message['data']
