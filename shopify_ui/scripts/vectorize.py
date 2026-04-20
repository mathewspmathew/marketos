# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "psycopg2-binary",
#     "requests",
#     "google-genai",
#     "python-dotenv"
# ]
# ///

import os
import sys
import requests
import psycopg2
from dotenv import load_dotenv
from google import genai
from io import BytesIO

# Load environment variables
load_dotenv('.env') 
load_dotenv('../.env')

DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-2-preview")

if not DATABASE_URL or "<USER>" in DATABASE_URL:
    print("Error: A valid DATABASE_URL (Aiven Cloud) is required. Please update your .env file.")
    sys.exit(1)

if not GOOGLE_API_KEY:
    print("Error: GOOGLE_API_KEY not found in environment.")
    sys.exit(1)

# Initialize Google GenAI client
client = genai.Client(api_key=GOOGLE_API_KEY)

# psycopg2 does not support the ?schema= query param expected by Prisma, so we strip it.
if "?" in DATABASE_URL:
    db_url_clean = DATABASE_URL.split("?")[0]
else:
    db_url_clean = DATABASE_URL

def format_vector(vector: list[float]) -> str:
    """Format float array to pgvector string literal '[x,y,...]'"""
    return "[" + ",".join(map(str, vector)) + "]"

def get_multimodal_embedding(text: str, img_url: str = None) -> tuple[list[float], list[float]]:
    """Generate embeddings using Google Gemini Embedding 2."""
    text_emb = None
    img_emb = None

    # 1. Generate Text Embedding
    try:
        print(f"Generating text embedding using {EMBEDDING_MODEL}...")
        res = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config={'output_dimensionality': 768}
        )
        text_emb = res.embeddings[0].values
    except Exception as e:
        print(f"Failed to generate text embedding: {e}")

    # 2. Generate Image Embedding (if URL provided)
    if img_url:
        try:
            print(f"Generating image embedding for {img_url}...")
            img_response = requests.get(img_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            img_response.raise_for_status()
            
            # Use raw bytes for the image part
            res = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=genai.types.Part.from_bytes(data=img_response.content, mime_type="image/jpeg"),
                config={'output_dimensionality': 768}
            )
            img_emb = res.embeddings[0].values
        except Exception as e:
            print(f"Failed to generate image embedding: {e}")

    return text_emb, img_emb

def main():
    print(f"Vectorization script started using {EMBEDDING_MODEL}...")
    
    # Connect to PostgreSQL
    print(f"Connecting to database...")
    try:
        conn = psycopg2.connect(db_url_clean)
        conn.autocommit = True
        cursor = conn.cursor()
    except Exception as e:
        print(f"Failed to connect to DB: {e}")
        return

    # 1. Fetch products from ShopifyProduct
    print("\n--- Processing Shopify Products ---")
    cursor.execute('SELECT id, title, description, "imageUrl" FROM "ShopifyProduct" WHERE vectorized = false;')
    shopify_products = cursor.fetchall()
    process_batch(cursor, shopify_products, "ShopifyProduct")

    # 2. Fetch products from ScrapedProduct
    print("\n--- Processing Scraped Products ---")
    cursor.execute('SELECT id, title, description, "imageUrl" FROM "ScrapedProduct" WHERE vectorized = false;')
    scraped_products = cursor.fetchall()
    process_batch(cursor, scraped_products, "ScrapedProduct")

    print("\nVectorization process finished.")
    cursor.close()
    conn.close()

def process_batch(cursor, products, table_name):
    if not products:
        print(f"No {table_name} found that need vectorization.")
        return

    print(f"Found {len(products)} {table_name} to vectorize.")

    for product in products:
        prod_id, title, desc, img_url = product
        print(f"\nProcessing {table_name}: {title} (ID: {prod_id})")

        combined_text = f"Title: {title}\nDescription: {desc or ''}"
        text_emb, img_emb = get_multimodal_embedding(combined_text, img_url)

        # Update Database
        if text_emb or img_emb:
            try:
                updates = ['vectorized = true']
                params = []
                
                if text_emb:
                    updates.append('"textEmbedding" = %s::vector')
                    params.append(format_vector(text_emb))
                if img_emb:
                    updates.append('"imageEmbedding" = %s::vector')
                    params.append(format_vector(img_emb))
                
                sql = f'UPDATE "{table_name}" SET {", ".join(updates)} WHERE id = %s;'
                params.append(prod_id)
                
                cursor.execute(sql, tuple(params))
                print(f"Successfully updated embeddings for {prod_id}.")
            except Exception as e:
                print(f"Failed to update database for {prod_id}: {e}")
        else:
            print(f"No embeddings generated for {prod_id}, skipping update.")

if __name__ == "__main__":
    main()
