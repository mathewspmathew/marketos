# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "psycopg2-binary",
#     "requests",
#     "transformers",
#     "pillow",
#     "python-dotenv",
#     "torch"
# ]
# ///

import os
import requests
import psycopg2
from dotenv import load_dotenv
from transformers import CLIPProcessor, CLIPModel
from PIL import Image
from io import BytesIO

# Load environment variables.
load_dotenv('.env') 
load_dotenv('../.env')

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Fallback to local docker-compose defaults if .env not loaded correctly
    DATABASE_URL = "postgresql://marketos:password123@localhost:5433/marketos?schema=public"

# psycopg2 does not support the ?schema= query param expected by Prisma, so we strip it.
if "?" in DATABASE_URL:
    db_url_clean = DATABASE_URL.split("?")[0]
else:
    db_url_clean = DATABASE_URL

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
NOMIC_MODEL = "nomic-embed-text"

def get_nomic_embedding(text: str) -> list[float]:
    """Fetch text embedding from local Ollama endpoint."""
    url = f"{OLLAMA_URL}/api/embeddings"
    payload = {
        "model": NOMIC_MODEL,
        "prompt": text
    }
    response = requests.post(url, json=payload)
    response.raise_for_status()
    data = response.json()
    return data["embedding"]

def format_vector(vector: list[float]) -> str:
    """Format float array to pgvector string literal '[x,y,...]'"""
    return "[" + ",".join(map(str, vector)) + "]"

def main():
    print("Vectorization script started. Loading models...")
    
    # Load OpenAI CLIP
    model_id = "openai/clip-vit-base-patch32"
    model = CLIPModel.from_pretrained(model_id)
    processor = CLIPProcessor.from_pretrained(model_id)
    print("OpenAI CLIP model loaded successfully.")

    # Connect to PostgreSQL
    print(f"Connecting to database...")
    try:
        conn = psycopg2.connect(db_url_clean)
        conn.autocommit = True
        cursor = conn.cursor()
    except Exception as e:
        print(f"Failed to connect to DB at {db_url_clean}: {e}")
        return

    # Check for pgvector extension
    cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    # Fetch products that need vectorization
    cursor.execute('SELECT id, title, description, "imageUrl" FROM "Product" WHERE vectorized = false;')
    products = cursor.fetchall()

    if not products:
        print("No products found that need vectorization.")
        return

    print(f"Found {len(products)} products to vectorize.")

    for product in products:
        prod_id, title, desc, img_url = product
        print(f"\nProcessing product: {title} (ID: {prod_id})")

        text_emb = None
        img_emb = None

        # 1. Text Embedding (Nomic via Ollama)
        combined_text = f"Title: {title}\nDescription: {desc or ''}"
        try:
            print(f"Generating text embedding via Ollama ({NOMIC_MODEL})...")
            text_emb = get_nomic_embedding(combined_text)
        except Exception as e:
            print(f"Failed to generate text embedding for {prod_id}: {e}")

        # 2. Image Embedding (CLIP)
        if img_url:
            print(f"Generating image embedding for {img_url} via CLIP...")
            try:
                img_response = requests.get(img_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
                img_response.raise_for_status()
                image = Image.open(BytesIO(img_response.content)).convert("RGB")
                
                import torch
                with torch.no_grad():
                    inputs = processor(images=image, return_tensors="pt")
                    image_features = model.get_image_features(**inputs)
                    
                    # Handle cases where it returns an output object instead of a direct tensor
                    if hasattr(image_features, "pooler_output"):
                        image_features = image_features.pooler_output
                        
                    img_emb = image_features.cpu().numpy()[0].tolist()
            except Exception as e:
                print(f"Failed to generate image embedding for {img_url}: {e}")
        else:
            print("No imageUrl found, skipping image embedding.")

        # 3. Update Database
        if text_emb or img_emb:
            try:
                sql = 'UPDATE "Product" SET vectorized = true'
                params = []
                if text_emb:
                    sql += ', "textEmbedding" = %s::vector'
                    params.append(format_vector(text_emb))
                if img_emb:
                    sql += ', "imageEmbedding" = %s::vector'
                    params.append(format_vector(img_emb))
                
                sql += ' WHERE id = %s;'
                params.append(prod_id)
                
                cursor.execute(sql, tuple(params))
                print(f"Successfully updated embeddings for {prod_id}.")
            except Exception as e:
                print(f"Failed to update database for {prod_id}: {e}")
        else:
            print(f"No embeddings generated for {prod_id}, skipping update.")

    print("\nVectorization process finished.")
    cursor.close()
    conn.close()

if __name__ == "__main__":
    main()
