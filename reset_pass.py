import mysql.connector

try:
    print("Connecting as root...")
    conn = mysql.connector.connect(
        host="127.0.0.1",
        user="root",
        password="root123" 
    )
    cursor = conn.cursor()
    
    print("Resetting analyzer_ro password...")
    cursor.execute("ALTER USER 'analyzer_ro'@'localhost' IDENTIFIED BY 'my_new_password';")
    cursor.execute("ALTER USER 'analyzer_ro'@'127.0.0.1' IDENTIFIED BY 'my_new_password';")
    cursor.execute("FLUSH PRIVILEGES;")
    
    print("✅ Success! Local password is now 'my_new_password'")
except Exception as e:
    print("❌ Error:", e)
