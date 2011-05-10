#
# A custom server that speeds up development time in Android significantly
#
import os, sys, time, optparse
import tcpserver, urllib
import simplejson, threading
import SocketServer, socket, struct, codecs
import logging

logging.basicConfig(format='[%(levelname)s] [%(asctime)s] %(message)s', level=logging.INFO)

support_android_dir = os.path.dirname(os.path.abspath(__file__))
support_dir = os.path.dirname(support_android_dir)
sys.path.append(support_dir)

import tiapp

server = None
request_count = 0
start_time = time.time()
idle_thread = None

def pack_int(i):
	return struct.pack("!i", i)

def send_tokens(socket, *tokens):
	buffer = pack_int(len(tokens))
	for token in tokens:
		buffer += pack_int(len(token))
		buffer += token
	socket.send(buffer)

def read_int(socket):
	data = socket.recv(4)
	if not data: return None
	return struct.unpack("!i", data)[0]

def read_tokens(socket):
	token_count = read_int(socket)
	if token_count == None: return None
	tokens = []
	for i in range(0, token_count):
		length = read_int(socket)
		data = socket.recv(length)
		tokens.append(data)
	return tokens

utf8_codec = codecs.lookup("utf-8")

""" A simple idle checker thread """
class IdleThread(threading.Thread):
	def __init__(self, max_idle_time):
		super(IdleThread, self).__init__()
		self.idle_time = 0
		self.max_idle_time = max_idle_time
		self.running = True

	def clear_idle_time(self):
		self.idle_lock.acquire()
		self.idle_time = 0
		self.idle_lock.release()

	def run(self):
		self.idle_lock = threading.Lock()
		while self.running:
			if self.idle_time < self.max_idle_time:
				time.sleep(1)
				self.idle_lock.acquire()
				self.idle_time += 1
				self.idle_lock.release()
			else:
				logging.info("Shutting down Fastdev server due to idle timeout: %s" % self.idle_time)
				server.shutdown()
				self.running = False

"""
A handler for fastdev requests.

The fastdev server uses a simple binary protocol comprised of messages and tokens.

Without a valid handshake, no requests will be processed.
Currently supported commands are:
	- "handshake" <guid> : Application handshake
	- "script-handshake" <guid> : Script control handshake
	- "get" <Resources relative path> : Get the contents of a file from the Resources folder
	- "kill-app" : Kills the connected app's process
	- "restart-app" : Restarts the connected app's process
	-"shutdown" : Shuts down the server

Right now the VFS rules for "get" are:
- Anything under "Resources" is served as is
- Anything under "Resources/android" overwrites anything under "Resources" (and is mapped to the root)
"""
class FastDevHandler(SocketServer.BaseRequestHandler):
	resources_dir = None
	handshake = None
	app_handler = None

	def handle(self):
		logging.info("connected: %s:%d" % self.client_address)
		global request_count
		self.valid_handshake = False
		self.request.settimeout(1.0)
		while True:
			try:
				tokens = read_tokens(self.request)
				if tokens == None:
					break
			except socket.timeout, e:
				# only break the loop when not serving, otherwise timeouts are normal
				if not server.is_serving():
					break
				else: continue

			idle_thread.clear_idle_time()
			command = tokens[0]
			if command == "handshake":
				FastDevHandler.app_handler = self
				self.handle_handshake(tokens[1])
			elif command == "script-handshake":
				self.handle_handshake(tokens[1])
			else:
				if not self.valid_handshake:
					self.send_tokens("Invalid Handshake")
					break
				if command == "get":
					request_count += 1
					self.handle_get(tokens[1])
				elif command == "kill-app":
					self.handle_kill_app()
					break
				elif command == "restart-app":
					self.handle_restart_app()
					break
				elif command == "status":
					self.handle_status()
				elif command == "shutdown":
					self.handle_shutdown()
					break
		logging.info("disconnected: %s:%d" % self.client_address)

	def handle_handshake(self, handshake):
		logging.info("handshake: %s" % handshake)
		if handshake == self.handshake:
			self.send_tokens("OK")
			self.valid_handshake = True
		else:
			logging.warn("handshake: invalid handshake sent, rejecting")
			self.send_tokens("Invalid Handshake")

	def handle_get(self, relative_path):
		android_path = os.path.join(self.resources_dir, 'android', relative_path)
		path = os.path.join(self.resources_dir, relative_path)
		if os.path.exists(android_path):
			logging.info("get %s: %s" % (relative_path, android_path))
			self.send_file(android_path)
		elif os.path.exists(path):
			logging.info("get %s: %s" % (relative_path, path))
			self.send_file(path)
		else:
			logging.warn("get %s: path not found" % relative_path)
			self.send_tokens("NOT_FOUND")

	def send_tokens(self, *tokens):
		send_tokens(self.request, *tokens)

	def send_file(self, path):
		buffer = open(path, 'r').read()
		self.send_tokens(buffer)

	def handle_kill_app(self):
		logging.info("request: kill-app")
		if FastDevHandler.app_handler != None:
			try:
				FastDevHandler.app_handler.send_tokens("kill")
				self.send_tokens("OK")
			except Exception, e:
				logging.error("kill: error: %s" % e)
				self.send_tokens(str(e))
		else:
			self.send_tokens("App not connected")
			logging.warn("kill: no app is connected")

	def handle_restart_app(self):
		logging.info("request: restart-app")
		if FastDevHandler.app_handler != None:
			try:
				FastDevHandler.app_handler.send_tokens("restart")
				self.send_tokens("OK")
			except Exception, e:
				logging.error("restart: error: %s" % e)
				self.send_tokens(str(e))
		else:
			self.send_tokens("App not connected")
			logging.warn("restart: no app is connected")

	def handle_status(self):
		logging.info("request: status")
		global server
		global request_count
		global start_time
		app_connected = FastDevHandler.app_handler != None
		status = {
			"uptime": int(time.time() - start_time),
			"pid": os.getpid(),
			"app_connected": app_connected,
			"request_count": request_count,
			"port": server.server_address[1]
		}
		self.send_tokens(simplejson.dumps(status))

	def handle_shutdown(self):
		self.send_tokens("OK")
		server.shutdown()
		idle_thread.running = False

class ThreadingTCPServer(SocketServer.ThreadingMixIn, tcpserver.TCPServer): pass

class FastDevRequest(object):
	def __init__(self, dir, options):
		self.lock_file = get_lock_file(dir, options)
		if not os.path.exists(self.lock_file):
			print >>sys.stderr, "Error: No Fastdev Servers found. " \
				"The lock file at %s does not exist, you either need to run \"stop\" " \
				"within your Titanium project or specify the lock file with -l <lock file>" \
				% self.lock_file
			sys.exit(1)
	
		f = open(self.lock_file, 'r')
		self.data = simplejson.loads(f.read())
		f.close()

		self.port = self.data["port"]
		self.app_guid = self.data["app_guid"]

		self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.socket.connect((socket.gethostname(), self.port))
		send_tokens(self.socket, "script-handshake", self.app_guid)
		response = read_tokens(self.socket)[0]
		if response != "OK":
			print >>sys.stderr, "Error: Handshake was not accepted by the Fastdev server"
			sys.exit(1)

	def send(self, *tokens):
		send_tokens(self.socket, *tokens)
		return read_tokens(self.socket)

	def close(self):
		self.socket.close()

def get_lock_file(dir, options):
	lock_file = options.lock_file
	if lock_file == None:
		lock_file = os.path.join(dir, ".fastdev.lock")
	return lock_file

def start_server(dir, options):
	xml = tiapp.TiAppXML(os.path.join(dir, "tiapp.xml"))
	app_id = xml.properties["id"]
	app_guid = xml.properties["guid"]

	lock_file = get_lock_file(dir, options)
	if os.path.exists(lock_file):
		print "Fastdev server already running for %s" % app_id
		sys.exit(0)

	resources_dir = os.path.join(dir, 'Resources')
	FastDevHandler.resources_dir = resources_dir
	FastDevHandler.handshake = app_guid

	global server
	global idle_thread
	server = ThreadingTCPServer(("", int(options.port)), FastDevHandler)
	port = server.server_address[1]
	logging.info("Serving up files for %s at 0.0.0.0:%d from %s" % (app_id, port, dir))

	f = open(lock_file, 'w+')
	f.write(simplejson.dumps({
		"ip": "0.0.0.0",
		"port": port,
		"dir": dir,
		"app_id": app_id,
		"app_guid": app_guid
	}))
	f.close()

	try:
		idle_thread = IdleThread(int(options.timeout))
		idle_thread.start()
		server.serve_forever()
	except KeyboardInterrupt, e:
		idle_thread.running = False
		server.shutdown_noblock()
		print "Terminated"

	logging.info("Fastdev server stopped.")
	os.unlink(lock_file)

def stop_server(dir, options):
	request = FastDevRequest(dir, options)
	print request.send("shutdown")[0]
	request.close()

	print "Fastdev server for %s stopped." % request.data["app_id"]

def kill_app(dir, options):
	request = FastDevRequest(dir, options)
	result = request.send("kill-app")
	request.close()

	if result and result[0] == "OK":
		print "Killed app %s." % request.data["app_id"]
		return True
	else:
		print "Error killing app, result: %s" % result
		return False

def restart_app(dir, options):
	request = FastDevRequest(dir, options)
	result = request.send("restart-app")
	request.close()

	if result and result[0] == "OK":
		print "Restarted app %s." % request.data["app_id"]
		return True
	else:
		print "Error restarting app, result: %s" % result
		return False

def is_running(dir):
	class Options(object): pass
	options = Options()
	options.lock_file = os.path.join(dir, '.fastdev.lock')

	if not os.path.exists(options.lock_file):
		return False

	try:
		request = FastDevRequest(dir, options)
		result = request.send("status")[0]
		request.close()
		status = simplejson.loads(result)
		return type(status) == dict
	except Exception, e:
		return False

def status(dir, options):
	lock_file = get_lock_file(dir, options)

	if lock_file == None or not os.path.exists(lock_file):
		print "No Fastdev servers running in %s" % dir
	else:
		data = simplejson.loads(open(lock_file, 'r').read())
		port = data["port"]
		try:
			request = FastDevRequest(dir, options)
			result = request.send("status")[0]
			request.close()
			status = simplejson.loads(result)

			print "Fastdev server running for app %s:" % data["app_id"]
			print "Port: %d" % port
			print "Uptime: %d sec" % status["uptime"]
			print "PID: %d" % status["pid"]
			print "Requests: %d" % status["request_count"]
		except Exception, e:
			print >>sys.stderr, "Error: .fastdev.lock found in %s, but couldn't connect to the server on port %d: %s. Try manually deleting .fastdev.lock." % (dir, port, e)

def get_optparser():
	usage = """Usage: %prog [command] [options] [app-dir]

Supported Commands:
	start		start the fastdev server
	status		get the status of the fastdev server
	stop		stop the fastdev server
	restart-app	restart the app connected to this fastdev server
	kill-app	kill the app connected to this fastdev server
"""
	parser = optparse.OptionParser(usage)
	parser.add_option('-p', '--port', dest='port',
		help='port to bind the server to [default: first available port]', default=0)
	parser.add_option('-t', '--timeout', dest='timeout',
		help='Timeout in seconds before the Fastdev server shuts itself down when it hasn\'t received a request [default: %default]',
		default=30 * 60)
	parser.add_option('-l', '--lock-file', dest='lock_file',
		help='Path to the server lock file [default: app-dir/.fastdev.lock]',
		default=None)
	return parser

def main():
	parser = get_optparser()
	(options, args) = parser.parse_args()

	if len(args) == 0 or args[0] not in ['start', 'stop', 'kill-app', 'restart-app', 'status']:
		parser.error("Missing required command")
		sys.exit(1)

	command = args[0]

	dir = os.getcwd()
	if len(args) > 1:
		dir = os.path.expanduser(args[1])

	dir = os.path.abspath(dir)

	if command == "start":
		if not os.path.exists(os.path.join(dir, "tiapp.xml")):
			parser.error("Directory is not a Titanium Project: %s" % dir)
			sys.exit(1)
		try:
			start_server(dir, options)
		except Exception, e:
			print >>sys.stderr, "Error starting Fastdev server: %s" % e

	elif command == "stop":
		stop_server(dir, options)
	elif command == "kill-app":
		kill_app(dir, options)
	elif command == 'restart-app':
		restart_app(dir, options)
	elif command == "status":
		status(dir, options)

if __name__ == "__main__":
	main()