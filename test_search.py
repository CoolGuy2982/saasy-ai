from google import genai
from google.genai import types
import os
from dotenv import load_dotenv

load_dotenv()
client = genai.Client()

config = types.GenerateContentConfig(
    tools=[types.Tool(google_search=types.GoogleSearch())],
)

response = client.models.generate_content(
    model="gemini-3-flash-preview",
    contents="what is nano banana AI try on clothes",
    config=config,
)

if response.candidates and response.candidates[0].grounding_metadata:
    gm = response.candidates[0].grounding_metadata
    print(f"grounding_chunks: {getattr(gm, 'grounding_chunks', None)}")
    if hasattr(gm, 'grounding_chunks') and gm.grounding_chunks:
        for p in gm.grounding_chunks:
            print(p)
    else:
        print(f"web_search_queries: {getattr(gm, 'web_search_queries', None)}")
else:
    print("No grounding metadata")
