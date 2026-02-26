from flask import Flask, render_template, request, jsonify
import sqlite3
import requests
import json
import os

app = Flask(__name__)

# NEU: Wir machen den Datenbank-Pfad flexibel für Docker!
# Wenn wir in Docker sind, nutzen wir einen speziellen Ordner, ansonsten normal lokal.
DB_PATH = os.environ.get('DB_PATH', 'einkaufsliste.db')

# Datenbank initialisieren
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Tabelle für die Listen
    c.execute('''
        CREATE TABLE IF NOT EXISTS listen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        )
    ''')
    
    # Standard-Liste erstellen, falls noch keine existiert
    c.execute('SELECT count(*) FROM listen')
    if c.fetchone()[0] == 0:
        c.execute('INSERT INTO listen (name) VALUES ("Meine Einkaufsliste")')

    c.execute('''
        CREATE TABLE IF NOT EXISTS produkte (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            is_food BOOLEAN DEFAULT 0,
            image_url TEXT,
            nutriments TEXT,
            erledigt BOOLEAN DEFAULT 0
        )
    ''')
    
    # Tabelle für lokal gespeicherte (selbst benannte) Produkte
    c.execute('''
        CREATE TABLE IF NOT EXISTS custom_products (
            barcode TEXT PRIMARY KEY,
            name TEXT NOT NULL
        )
    ''')
    
    # MIGRATIONEN für ältere Datenbank-Versionen
    try:
        c.execute('ALTER TABLE produkte ADD COLUMN menge INTEGER DEFAULT 1')
    except sqlite3.OperationalError:
        pass 
        
    try:
        c.execute('ALTER TABLE produkte ADD COLUMN liste_id INTEGER DEFAULT 1')
    except sqlite3.OperationalError:
        pass 

    try:
        c.execute('ALTER TABLE produkte ADD COLUMN ingredients TEXT')
    except sqlite3.OperationalError:
        pass 

    try:
        c.execute('ALTER TABLE produkte ADD COLUMN barcode TEXT')
    except sqlite3.OperationalError:
        pass 

    # Spalten für Hersteller und Marke
    try:
        c.execute('ALTER TABLE produkte ADD COLUMN brand TEXT')
    except sqlite3.OperationalError:
        pass 
    
    try:
        c.execute('ALTER TABLE produkte ADD COLUMN manufacturer TEXT')
    except sqlite3.OperationalError:
        pass 

    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# --- ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

# --- LISTEN ROUTES ---
@app.route('/api/lists', methods=['GET'])
def get_lists():
    conn = get_db_connection()
    lists = conn.execute('SELECT * FROM listen').fetchall()
    conn.close()
    return jsonify([dict(l) for l in lists])

@app.route('/api/lists', methods=['POST'])
def add_list():
    data = request.json
    name = data.get('name', 'Neue Liste')
    conn = get_db_connection()
    cursor = conn.execute('INSERT INTO listen (name) VALUES (?)', (name,))
    conn.commit()
    list_id = cursor.lastrowid
    conn.close()
    return jsonify({'id': list_id, 'name': name})

@app.route('/api/lists/<int:list_id>', methods=['DELETE'])
def delete_list(list_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM produkte WHERE liste_id = ?', (list_id,))
    conn.execute('DELETE FROM listen WHERE id = ?', (list_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# --- PRODUKT ROUTES ---
@app.route('/api/list/<int:liste_id>', methods=['GET'])
def get_list_items(liste_id):
    conn = get_db_connection()
    items = conn.execute('SELECT * FROM produkte WHERE liste_id = ?', (liste_id,)).fetchall()
    conn.close()
    
    result = []
    for item in items:
        # Check keys for backwards compatibility if column was just added
        item_dict = dict(item)
        result.append({
            'id': item_dict['id'],
            'liste_id': item_dict['liste_id'],
            'name': item_dict['name'],
            'is_food': bool(item_dict['is_food']),
            'image_url': item_dict['image_url'],
            'nutriments': json.loads(item_dict['nutriments']) if item_dict['nutriments'] else None,
            'ingredients': item_dict['ingredients'],
            'brand': item_dict.get('brand'),
            'manufacturer': item_dict.get('manufacturer'),
            'erledigt': bool(item_dict['erledigt']),
            'menge': item_dict['menge']
        })
    return jsonify(result)

@app.route('/api/list/<int:liste_id>', methods=['POST'])
def add_manual_item(liste_id):
    data = request.json
    name = data.get('name')
    menge = data.get('menge', 1) 
    
    if not name:
        return jsonify({'error': 'Name fehlt'}), 400
        
    conn = get_db_connection()
    existing = conn.execute('SELECT id, menge FROM produkte WHERE name = ? AND liste_id = ?', (name, liste_id)).fetchone()
    
    if existing:
        new_menge = existing['menge'] + menge
        item_id = existing['id']
        conn.execute('UPDATE produkte SET menge = ?, erledigt = 0 WHERE id = ?', (new_menge, item_id))
    else:
        cursor = conn.execute(
            'INSERT INTO produkte (liste_id, name, is_food, image_url, nutriments, ingredients, erledigt, menge, barcode, brand, manufacturer) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (liste_id, name, False, None, None, None, False, menge, None, None, None) 
        )
        item_id = cursor.lastrowid
        
    conn.commit()
    conn.close()
    
    return jsonify({'id': item_id, 'name': name, 'erledigt': False, 'is_food': False, 'menge': existing['menge'] + menge if existing else menge, 'liste_id': liste_id})

@app.route('/api/scan/<int:liste_id>', methods=['POST'])
def scan_item(liste_id):
    data = request.json
    barcode = data.get('barcode')
    menge = data.get('menge', 1) 
    
    if not barcode:
        return jsonify({'error': 'Barcode fehlt'}), 400

    conn = get_db_connection()
    custom_item = conn.execute('SELECT name FROM custom_products WHERE barcode = ?', (barcode,)).fetchone()

    is_food = False
    image_url = None
    nutriments_data = None
    ingredients_text = None
    brand = None
    manufacturer = None

    if custom_item:
        name = custom_item['name']
    else:
        name = f"Unbekanntes Produkt ({barcode})"

        # 1. Check OpenFoodFacts (Lebensmittel)
        try:
            off_response = requests.get(f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json", timeout=5)
            off_data = off_response.json()
            
            if off_data.get('status') == 1:
                product = off_data.get('product', {})
                name = product.get('product_name', name)
                image_url = product.get('image_front_url', None)
                is_food = True
                ingredients_text = product.get('ingredients_text_de') or product.get('ingredients_text', None)
                brand = product.get('brands', None)
                manufacturer = product.get('manufacturing_places', None)
                
                nutriments = product.get('nutriments', {})
                nutriments_data = {
                    'energy': nutriments.get('energy-kcal_100g', '-'),
                    'fat': nutriments.get('fat_100g', '-'),
                    'carbohydrates': nutriments.get('carbohydrates_100g', '-'),
                    'proteins': nutriments.get('proteins_100g', '-'),
                    'sugar': nutriments.get('sugars_100g', '-'),
                    'salt': nutriments.get('salt_100g', '-')
                }
        except Exception as e:
            print(f"Fehler bei OpenFoodFacts: {e}")

        # 2. Check OpenBeautyFacts (Kosmetik, Hygiene, Drogerie)
        if name == f"Unbekanntes Produkt ({barcode})":
            try:
                obf_response = requests.get(f"https://world.openbeautyfacts.org/api/v0/product/{barcode}.json", timeout=5)
                obf_data = obf_response.json()
                
                if obf_data.get('status') == 1:
                    product = obf_data.get('product', {})
                    name = product.get('product_name', name)
                    image_url = product.get('image_front_url', None)
                    ingredients_text = product.get('ingredients_text_de') or product.get('ingredients_text', None)
                    brand = product.get('brands', None)
                    manufacturer = product.get('manufacturing_places', None)
            except Exception as e:
                print(f"Fehler bei OpenBeautyFacts: {e}")

        # 3. Check OpenPetFoodFacts (Tiernahrung)
        if name == f"Unbekanntes Produkt ({barcode})":
            try:
                opff_response = requests.get(f"https://world.openpetfoodfacts.org/api/v0/product/{barcode}.json", timeout=5)
                opff_data = opff_response.json()
                
                if opff_data.get('status') == 1:
                    product = opff_data.get('product', {})
                    name = product.get('product_name', name)
                    image_url = product.get('image_front_url', None)
                    ingredients_text = product.get('ingredients_text_de') or product.get('ingredients_text', None)
                    brand = product.get('brands', None)
                    manufacturer = product.get('manufacturing_places', None)
            except Exception as e:
                print(f"Fehler bei OpenPetFoodFacts: {e}")

        # 4. Check OpenProductsFacts (Haushalt, Technik, Sonstiges)
        if name == f"Unbekanntes Produkt ({barcode})":
            try:
                opf_response = requests.get(f"https://world.openproductsfacts.org/api/v0/product/{barcode}.json", timeout=5)
                opf_data = opf_response.json()
                
                if opf_data.get('status') == 1:
                    product = opf_data.get('product', {})
                    name = product.get('product_name', name)
                    image_url = product.get('image_front_url', None)
                    brand = product.get('brands', None)
                    manufacturer = product.get('manufacturing_places', None)
            except Exception as e:
                print(f"Fehler bei OpenProductsFacts: {e}")

        # 5. Falls immer noch nichts gefunden, check UPCitemdb (Weltweiter Fallback)
        if name == f"Unbekanntes Produkt ({barcode})":
            try:
                upc_response = requests.get(f"https://api.upcitemdb.com/prod/trial/lookup?upc={barcode}", timeout=5)
                upc_data = upc_response.json()
                
                if upc_data.get('code') == 'OK' and len(upc_data.get('items', [])) > 0:
                    item = upc_data['items'][0]
                    name = item.get('title', name)
                    images = item.get('images', [])
                    if images:
                        image_url = images[0]
                    brand = item.get('brand', None)
                    manufacturer = item.get('manufacturer', None)
            except Exception as e:
                print(f"Fehler bei UPCitemdb: {e}")

    # In Datenbank speichern
    existing = conn.execute('SELECT id, menge FROM produkte WHERE name = ? AND liste_id = ?', (name, liste_id)).fetchone()
    
    if existing:
        new_menge = existing['menge'] + menge
        item_id = existing['id']
        conn.execute('UPDATE produkte SET menge = ?, erledigt = 0 WHERE id = ?', (new_menge, item_id))
    else:
        nutriments_json = json.dumps(nutriments_data) if nutriments_data else None
        cursor = conn.execute(
            'INSERT INTO produkte (liste_id, name, is_food, image_url, nutriments, ingredients, erledigt, menge, barcode, brand, manufacturer) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (liste_id, name, is_food, image_url, nutriments_json, ingredients_text, False, menge, barcode, brand, manufacturer) 
        )
        item_id = cursor.lastrowid
        
    conn.commit()
    conn.close()

    return jsonify({
        'id': item_id,
        'liste_id': liste_id,
        'name': name,
        'is_food': is_food,
        'image_url': image_url,
        'nutriments': nutriments_data,
        'ingredients': ingredients_text,
        'brand': brand,
        'manufacturer': manufacturer,
        'erledigt': False,
        'menge': existing['menge'] + menge if existing else menge,
        'is_unknown': name == f"Unbekanntes Produkt ({barcode})"
    })

@app.route('/api/item/<int:item_id>', methods=['PUT'])
def update_item(item_id):
    data = request.json
    conn = get_db_connection()
    
    if 'erledigt' in data:
        conn.execute('UPDATE produkte SET erledigt = ? WHERE id = ?', (data['erledigt'], item_id))
    
    if 'name' in data:
        new_name = data['name']
        conn.execute('UPDATE produkte SET name = ? WHERE id = ?', (new_name, item_id))
        
        item = conn.execute('SELECT barcode FROM produkte WHERE id = ?', (item_id,)).fetchone()
        if item and item['barcode']:
            conn.execute('INSERT OR REPLACE INTO custom_products (barcode, name) VALUES (?, ?)', (item['barcode'], new_name))
            
    if 'menge' in data:
        conn.execute('UPDATE produkte SET menge = ? WHERE id = ?', (data['menge'], item_id))
        
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/item/<int:item_id>', methods=['DELETE'])
def delete_item(item_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM produkte WHERE id = ?', (item_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)