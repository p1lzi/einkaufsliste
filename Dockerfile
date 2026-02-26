# Wir nutzen ein schlankes Python 3.11 Image
FROM python:3.11-slim

# Setze das Arbeitsverzeichnis im Container
WORKDIR /app

# Kopiere die requirements.txt in den Container und installiere die Pakete
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiere den restlichen Code (app.py, templates Ordner) in den Container
COPY . .

# Erstelle einen Ordner für die Datenbank, den wir später sichern (Volume)
RUN mkdir -p /app/data

# Umgebungsvariablen definieren
ENV DB_PATH=/app/data/einkaufsliste.db
# Standard-Port festlegen (kann beim Starten des Containers überschrieben werden)
ENV PORT=5000

# Den gewählten Port freigeben
EXPOSE $PORT

# Starte die App
CMD ["python", "app.py"]