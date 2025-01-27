import base64
import requests
import os
import mysql.connector
from flask import current_app, g
import time
from mysql.connector import pooling, Error as MySQLError

# Read Akeyless configuration from environment variables
AKEYLESS_K8S_AUTH_ID = os.environ.get('AKEYLESS_K8S_AUTH_ID')
AKEYLESS_GATEWAY_URL = os.environ.get('AKEYLESS_GATEWAY_URL')
AKEYLESS_GATEWAY_API_URL = os.environ.get('AKEYLESS_GATEWAY_API_URL')
AKEYLESS_K8S_AUTH_CONFIG_NAME = os.environ.get('AKEYLESS_K8S_AUTH_CONFIG_NAME')
DATABASE_DYNAMIC_SECRET_NAME = os.environ.get('DATABASE_DYNAMIC_SECRET_NAME')

# URLs for Akeyless Gateway
AUTH_URL = f"{AKEYLESS_GATEWAY_API_URL}/auth"
SECRET_URL = f"{AKEYLESS_GATEWAY_API_URL}/get-dynamic-secret-value"

# Load the service account token
def get_k8s_service_account_token():
    if os.environ.get('ENVIRONMENT') == 'remote':
        with open('/var/run/secrets/kubernetes.io/serviceaccount/token', 'r') as file:
            return file.read().strip()
    else:
        return os.environ.get('K8S_SERVICE_ACCOUNT_TOKEN')

# Authenticate with Akeyless using Kubernetes auth
def authenticate_with_akeyless():
    k8s_service_account_token = get_k8s_service_account_token()
    
    # Debug logging
    print("Debug: Service Account Token:", k8s_service_account_token[:20] + "..." if k8s_service_account_token else "None")
    print("Debug: Auth ID:", AKEYLESS_K8S_AUTH_ID)
    print("Debug: Gateway URL:", AKEYLESS_GATEWAY_URL)
    print("Debug: Auth Config Name:", AKEYLESS_K8S_AUTH_CONFIG_NAME)
    print("Debug: Auth URL:", AUTH_URL)
    
    payload = {
        "access-type": "k8s",
        "json": True,
        "access-id": AKEYLESS_K8S_AUTH_ID,
        "debug": True,
        "gateway-url": AKEYLESS_GATEWAY_URL,
        "k8s-auth-config-name": AKEYLESS_K8S_AUTH_CONFIG_NAME,
        "k8s-service-account-token": base64.b64encode(k8s_service_account_token.encode()).decode(),
    }
    
    # Debug logging
    print("Debug: Full Payload:", payload)
    
    headers = {
        "accept": "application/json",
        "content-type": "application/json"
    }
    
    try:
        response = requests.post(AUTH_URL, json=payload, headers=headers)
        print("Debug: Response Status:", response.status_code)
        print("Debug: Response Body:", response.text)
        response.raise_for_status()
        return response.json().get('token')
    except requests.exceptions.RequestException as e:
        print("Debug: Request Exception:", str(e))
        raise

# Retrieve the dynamic secret
def get_dynamic_secret(token):
    payload = {
        "json": True,
        "timeout": 15,
        "name": DATABASE_DYNAMIC_SECRET_NAME,
        "token": token
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/json"
    }
    if os.environ.get('ENVIRONMENT') == 'remote':
        response = requests.post(SECRET_URL, json=payload, headers=headers)
    else:
        response = requests.post(SECRET_URL, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()

# Add connection pool configuration
POOL_CONFIG = {
    "pool_name": "mypool",
    "pool_size": 5,
    "pool_reset_session": True
}

# Add retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds

class RetryableConnectionPool:
    def __init__(self, app):
        self.app = app
        self.pool = None
        self.last_refresh = 0
        # Convert "15s" to integer seconds
        ttl_str = os.environ.get('DYNAMIC_SECRET_TTL', '15s')
        self.credential_ttl = int(ttl_str.rstrip('s'))
        self.init_pool()

    def should_refresh(self):
        """Check if credentials are about to expire"""
        return time.time() - self.last_refresh >= (self.credential_ttl - 2)  # refresh 2s before expiry

    def init_pool(self):
        """Initialize or reinitialize the connection pool with fresh credentials"""
        try:
            token = authenticate_with_akeyless()
            secret = get_dynamic_secret(token)
            pool_config = POOL_CONFIG.copy()
            pool_config.update({
                'host': os.environ.get('DB_HOST', 'localhost'),
                'user': secret['user'],
                'password': secret['password'],
                'database': os.environ.get('DB_NAME', 'todos')
            })
            if self.pool:
                # Close all existing connections before creating new pool
                self.pool._remove_connections()
            self.pool = mysql.connector.pooling.MySQLConnectionPool(**pool_config)
            self.last_refresh = time.time()
            print("\033[92mConnection pool initialized/refreshed with new credentials\033[0m")
            print(f"\033[91muser: {secret['user']}\033[0m")
        except Exception as e:
            print(f"\033[91mError initializing pool: {str(e)}\033[0m")
            raise

    def get_connection(self):
        """Get a connection with retry logic and credential validation"""
        if self.should_refresh():
            self.init_pool()

        for attempt in range(MAX_RETRIES):
            try:
                conn = self.pool.get_connection()
                # Validate connection with a simple query
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchall()  # Consume the result
                cursor.close()
                return conn
            except MySQLError as e:
                if "Access denied" in str(e) or "Connection refused" in str(e):
                    print(f"\033[93mAttempt {attempt + 1}/{MAX_RETRIES}: Refreshing connection pool due to: {str(e)}\033[0m")
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAY)
                        self.init_pool()
                    else:
                        raise Exception("Max retries reached for database connection")
                else:
                    raise

def init_db_pool(app):
    """Initialize the retryable connection pool"""
    with app.app_context():
        if not hasattr(app, 'db_pool'):
            app.db_pool = RetryableConnectionPool(app)
            print("Retryable database connection pool initialized")

def get_db():
    """Get a connection from the pool with retry logic"""
    if 'db' not in g:
        try:
            g.db = current_app.db_pool.get_connection()
        except Exception as e:
            print(f"\033[91mError getting database connection: {str(e)}\033[0m")
            raise
    return g.db

def close_db(e=None):
    """Close database connection"""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_app(app):
    init_db_pool(app)
    app.teardown_appcontext(close_db)
