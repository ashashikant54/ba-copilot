# This file tests that your OpenAI connection works
# We load the secret key and ask GPT-4o a simple question

from dotenv import load_dotenv
import os
from openai import OpenAI

# Step 1: Load your secret key from the .env file
load_dotenv()

# Step 2: Create a connection to OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Step 3: Send a message to GPT-4o-mini and get a response
print("Sending a message to GPT-4o-mini...")

response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {
            "role": "system",
            "content": "You are a helpful business analyst assistant."
        },
        {
            "role": "user", 
            "content": "Say hello and tell me in one sentence what a BRD is."
        }
    ]
)

# Step 4: Print the response
answer = response.choices[0].message.content
print("\nGPT-4o-mini says:")
print(answer)