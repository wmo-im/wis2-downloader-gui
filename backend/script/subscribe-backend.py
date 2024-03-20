from flask import Flask, request
import socket
import json
import logging
import os
import sys
import time
import paho.mqtt.client as mqtt
from pathlib import Path
import queue
import ssl
import threading
import urllib3
from urllib.parse import urlsplit
import argparse
from datetime import datetime as dt
import hashlib
import base64

# LOGGER
logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S"
)
LOGGER = logging.getLogger(__name__)

# Global variables
urlQ = queue.Queue()
http = urllib3.PoolManager()


def create_app(subs, download_dir, client):
    """
    Starts the Flask app and download worker thread, as well as
    enabling functionality for adding and deleting topics during
    an on-going subscription.

    Args:
        subs (dict): A dictionary of form {topic: download_directory, ...}.
        download_dir (str): The directory to download files to.
        client (mqtt.client): The client that connects to the broker
        and subscribes to the topics.

    Raises:
        FileNotFoundError: When the user specifies a download directory
        that does not exist or is not writable.

    Returns:
        Flask: The Flask app that allows one to add, delete, or list
        subscribed topics (and associated download directories)
        during the MQTT subscription.
    """
    LOGGER.debug("Creating app")
    # Create and configure the app
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY='dev',
        DATABASE=os.path.join(app.instance_path, 'flaskr.sqlite'),
    )

    # Check if the directory exists and is writable before
    # starting the download thread
    if not (os.path.exists(download_dir) and os.access(download_dir, os.W_OK)):  # noqa
        raise FileNotFoundError("Specified download directory does not exist or is not writable.")  # noqa

    # Set the number of worker threads to the number of CPU cores - 2
    num_cores = os.cpu_count()
    num_worker_threads = num_cores - 2

    # Start the download worker for each thread
    for _ in range(num_worker_threads):
        t = threading.Thread(
            target=download_worker, args=(subs, download_dir), daemon=True)
        t.start()

    @app.route('/wis2/subscriptions/list')
    def list_subscriptions():
        return subs

    @app.route('/wis2/subscriptions/add')
    def add_subscription():
        return handle_add_subscription(subs, download_dir, client)

    @app.route('/wis2/subscriptions/delete')
    def delete_subscription():
        return handle_delete_subscription(subs, client)

    return app


def get_expected_hash(job):
    """
    Returns the expected hash value from the job payload,
    which can be later compared to the actual hash value
    of the downloaded file.

    Args:
        job (dict): Contains the topic and payload.

    Returns:
        tuple: The hash method, expected hash value, and hash function.
    """
    if 'integrity' not in job['payload']['properties']:
        return

    method = job['payload']['properties']['integrity']['method']
    expected = job['payload']['properties']['integrity']['value']
    hash_function = getattr(hashlib, method, None)

    return expected, hash_function


def get_todays_date():
    """
    Returns today's date in the format yyyy/mm/dd.
    """
    today = dt.now()
    yyyy = f"{today.year:04}"
    mm = f"{today.month:02}"
    dd = f"{today.day:02}"

    return f"{yyyy}/{mm}/{dd}"


def download_worker(subs, download_dir):
    """
    Downloads the files from the queue and saves them to the
    correct directory.

    Args:
        subs (dict): The dictionary of topics and download directories.
        download_dir (str): The default directory to download files to, if
        a topic does not have an associated download directory for some reason.
    """
    # Declare global variables
    global urlQ
    global http

    # Continuously check for new jobs in the queue to download
    while True:
        LOGGER.debug(f"Messages in queue: {urlQ.qsize()}")
        job = urlQ.get()

        # Check for the hash
        hash_expected_value, hash_function = get_expected_hash(job)  # noqa

        # Determine the output directory (if for some reason a topic does
        # not have an associated download directory, use the default directory)
        output_dir = subs.get(job['topic'], download_dir)
        output_dir = Path(output_dir)

        # Prepare directory
        dataid = Path(job['payload']['properties']['data_id'])
        # We need to replace colons in output path
        dataid = Path(str(dataid).replace(":", ""))

        # Get date (used in output path due to number of files)
        today = get_todays_date()

        output_path = Path(output_dir, today, dataid)

        # Create directory
        output_path.parent.mkdir(exist_ok=True, parents=True)
        LOGGER.debug(f"Directory created at: {output_path.parent}")

        # Now download the files
        for link in job['payload']['links']:
            if link['rel'] == "canonical":
                download_and_save_file(link['href'], output_path, hash_expected_value, hash_function)  # noqa

        urlQ.task_done()


def compare_hashes(data, expected, hash_function):
    """
    Compares the hash of the downloaded file with the expected hash value.

    Args:
        data (dict): _description_
        hash_expected_value (str): The expected hash value of the file.
        hash_function (function): The hash function to use to hash the file.
    """
    if None in (hash_function, expected):
        LOGGER.debug("No hash function or expected hash found to compare")
        return

    hash_value = hash_function(data).digest()

    # Encode the hash to Base64
    hash_value_base64 = base64.b64encode(hash_value).decode('utf-8')

    if hash_value_base64 == expected:
        LOGGER.debug("Hashes match")
    else:
        LOGGER.error("Hashes do not match")


def download_and_save_file(url, output_path, hash_expected_value, hash_function):  # noqa
    """
    Downloads the file from the given URL and saves it to the
    given output path.

    Args:
        url (str): The URL of the file to download.
        output_path (Path): The path where the file should be saved.
        hash_expected_value (str): The expected hash value of the file.
        hash_function (function): The hash function to use to hash the file.
    """
    path = urlsplit(url).path
    filename = os.path.basename(path)
    LOGGER.info(f"Attempting to download {filename}")

    # If file already in output directory, do not download
    if output_path.is_file():
        LOGGER.info(f"File {filename} already downloaded. Skipping.")
        return

    download_start = dt.now()

    # Try to download and verify the file
    try:
        response = http.request("GET", url)
        # Get filesize in KB
        filesize = len(response.data) / 1024
        # Check if the hash matches the expected hash value
        compare_hashes(response.data, hash_expected_value, hash_function)
    except Exception as e:
        LOGGER.error(f"Error downloading {url}")
        LOGGER.error(e)

    # Try to save the file to disk
    try:
        output_path.write_bytes(response.data)
        download_end = dt.now()
        time_to_download = (download_end - download_start).total_seconds()
        LOGGER.info(f"Downloaded {filename} of size {round(filesize, 2)}KB in {round(time_to_download, 2)} seconds")  # noqa
    except Exception as e:
        LOGGER.error(f"Error saving to disk: {output_path}/{filename}")
        LOGGER.error(e)


def handle_add_subscription(subs, download_dir, client):
    """
    Subscribes the MQTT client to the new topic and updates the subscription
    dictionary with a new topic and download, provided the topic does not
    already exist.

    Args:
        subs (dict): A dictionary of form {topic: download_directory, ...}.
        download_dir (str): The directory to download files to.
        client (mqtt.client): The client that connects to the broker
        and subscribes to the topics.

    Returns:
        dict: The updated subscription dictionary.
    """
    topic = request.args.get('topic', None)
    if topic is None:
        return "No topic passed"
    else:
        if topic in subs:
            LOGGER.info(f"Topic {topic} already subscribed")
        else:
            client.subscribe(f"{topic}")
            subs[topic] = download_dir
    return subs


def handle_delete_subscription(subs, client):
    """
    Unsubscribes the MQTT client from the topic and updates the subscription
    dictionary accordingly, provided the topic does already exists.

    Args:
        subs (dict): A dictionary of form {topic: download_directory, ...}.
        client (mqtt.client): The client that connects to the broker
        and subscribes to the topics.

    Returns:
        dict: The updated subscription dictionary.
    """
    topic = request.args.get('topic', None)
    if topic is None:
        return "No topic passed"
    else:
        client.unsubscribe(f"{topic}")
        LOGGER.info(f"{topic}/#")
        if topic in subs:
            del subs[topic]
        else:
            LOGGER.info(f"Topic {topic} not found")
            for sub in subs:
                LOGGER.info(sub, topic)
    return subs


def get_config_data(args):
    """
    Get the configuration data used for the subscription. The
    location of the configuration file by default is the same
    as the path of the application (which depends on whether
    the application is run as a bundled executable or as a
    normal Python script). If the user specifies a different
    location for the configuration file, then that location
    will be used instead.

    Args:
        args (argparse.Namespace): The command-line arguments
        passed to the application.

    Returns:
        dict: The configuration data used for the subscription.
    """
    if getattr(sys, 'frozen', False):
        # If the application is run as a bundled executable,
        # the sys.executable path will be the path
        application_path = os.path.dirname(sys.executable)
    else:
        # If it's run as a normal Python script, get the
        # path of the script in the usual way
        application_path = os.path.dirname(os.path.realpath(__file__))

    if args.config is None:
        # From the base path get the path of the config file
        config_path = os.path.join(application_path, 'config.json')
    else:
        # If the user manually specifies a config file, use that
        config_path = args.config

    # Load configuration data
    with open(config_path, 'r') as f:
        config = json.load(f)

    return config


def on_connect(client, userdata, flags, rc):
    """
    When the MQTT client connects to the broker, this function is called.
    Note: These four arguments are required even if not explicitly used.

    Args:
        client (mqtt.client): The client that connects to the broker
        and subscribes to the topics.
        userdata (Any): The user data that is passed to the client
        when it connects to the broker.
        flags (dict): The response flags sent by the broker.
        rc (int): The result code returned when the client connects
        to the broker.
    """
    LOGGER.info("Connected")


def on_message(client, userdata, msg):
    """
    When the MQTT client receives a message, this function is called
    to inform the user of the message received and add the message
    to the queue.
    Note: These three arguments are required even if two aren't
    explicitly used.

    Args:
        client (mqtt.client): The client that connects to the broker.
        userdata (Any): The user data that is passed to the client 
        when a message is received.
        msg (dict): The message received from the MQTT client.
    """
    # Declare urlQ as global
    global urlQ

    LOGGER.info("Message received")

    # Create new job and add to queue
    job = {
        'topic': msg.topic,
        'payload': json.loads(msg.payload)
    }
    urlQ.put(job)


def on_subscribe(client, usedata, mid, granted_qos):
    """
    When the MQTT client subscribes to a topic, this function is called.
    Note: These four arguments are required even if not explicitly used.

    Args:
        client (mqtt.client): The client that connects to the broker.
        userdata (Any): The user data that is passed to the client
        when a subscription is started.
        mid (int): The message ID of the subscription message.
        granted_qos (list): The list of granted QoS levels for the
        requested topics.
    """
    LOGGER.debug(("On subscribe"))


def initialise_client():
    """
    Initialises the MQTT client using default connection credentials
    and the websockets protocol.
    Note: For paho-mqtt 2.0, the callback API version must be specified.
    To minimise changes to existing code, we use the VERSION1 API, which
    is supported but will produce a deprecation warning.

    Args:
        broker (str): The URL of the broker to connect to
        (e.g. globalbroker.meteo.fr).
        topics (list): A list of the topics to subscribe to.
        download_dir (str): The directory to download files to.

    Returns:

    """
    # Define login credentials
    pwd = "everyone"
    uid = "everyone"
    protocol = "websockets"

    LOGGER.debug("Initialising client")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, transport=protocol)
    client.tls_set(ca_certs=None, certfile=None, keyfile=None,
                   cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS,
                   ciphers=None)
    client.username_pw_set(uid, pwd)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_subscribe = on_subscribe

    return client


def find_open_port():
    """
    To avoid port conflicts, this function finds an open port
    dynamically for the Flask app to use.
    """
    # Create a socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Bind the socket to a random aviailable port
    # (0 means the OS will choose a random port for us)
    s.bind(('127.0.0.1', 0))

    # Get the port number
    port = s.getsockname()[1]

    # Close the socket
    s.close()

    return port


def log_queue_size_every_minute():
    """
    Every minute, log the size of the queue.
    """
    global urlQ
    while True:
        LOGGER.info(f"Current queue size: {urlQ.qsize()}")
        time.sleep(60)


def main():
    """
    Main function to start the subscription backend. It loads the
    configuration data and initialises the MQTT client. It then
    starts the MQTT thread and creates the Flask app to handle
    updates to an on-going subscription.
    """
    # Parse system argument: the directory of the configuration file
    parser = argparse.ArgumentParser(
        description="WIS2 Downloader Backend Configuration")
    parser.add_argument(
        "--config", default=None,
        help="The absolute directory of the configuration file")
    args = parser.parse_args()

    # Determine base path of application
    config = get_config_data(args)

    # Log configuration data loaded
    broker = config['broker']
    topics = config['topics']
    download_dir = config['download_directory']

    # Initialise MQTT client
    client = initialise_client()
    port = 443

    # Connect to the broker
    LOGGER.info("Connecting...")
    result = client.connect(host=broker, port=port)
    LOGGER.debug(result)

    # Start the MQTT thread
    mqtt_thread = threading.Thread(
        target=client.loop_forever, daemon=True)
    mqtt_thread.start()

    # For the subscription, the client expects topic and download directory
    # pairs. So we need to create a new object with the correct format
    # before subscribing to the topics.
    subs = {t: download_dir for t in topics}
    for sub in subs:
        client.subscribe(sub)

    # Try to create the Flask app
    try:
        app = create_app(subs, download_dir, client)
    except Exception as e:
        LOGGER.error(f"Error starting Flask app due to: {e}")

    # To prevent issues with reloading when the application is frozen
    # by Pyinstaller, we disable the reloader of the Flask app
    app.run(debug=True, use_reloader=False)

    # Start the queue size logger thread
    queue_logger_thread = threading.Thread(
        target=log_queue_size_every_minute, daemon=True)
    queue_logger_thread.start()


if __name__ == '__main__':
    main()
