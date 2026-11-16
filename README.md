# Mosca Pinta

Sistema de detección y conteo de mosca pinta (Aeneolamia spp.) en cultivos de
caña de azúcar mediante visión por computadora.

## Estructura
- `backend/`: servidor FastAPI + modelo YOLOv8s + base de datos SQLite.
- `app_flutter/`: aplicación web (PWA) hecha en Flutter.
- `entrenamiento/`: scripts de entrenamiento del modelo.

## Instalar y ejecutar el servidor
```
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Correo (verificación de cuenta)
Copia `correo_config.ejemplo.json` a `correo_config.json` y llena tus datos.
Sin ese archivo, el código de verificación aparece en la terminal del servidor.

## Modelo
Coloca el archivo `best.pt` junto a `main.py`.
