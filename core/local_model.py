"""Local model interface, wraps Ollama for private inference."""
import json
import urllib.request
from config import LOCAL_MODEL, EMBED_MODEL, OLLAMA_BASE_URL


def generate(prompt, system=None, max_tokens=500, model=None):
    """Run text generation on the local model."""
    payload = {
        "model": model or LOCAL_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens}
    }
    if system:
        payload["system"] = system
    
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode())
    return result.get("response", "")


def embed(text):
    """Get embeddings from nomic-embed-text."""
    payload = {
        "model": EMBED_MODEL,
        "input": text if isinstance(text, list) else [text]
    }
    
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/embed",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    return result.get("embeddings", [])


def classify(text, categories):
    """Classify text into one of the given categories using local model."""
    cats = ", ".join(categories)
    prompt = f"Classify the following text into exactly one of these categories: {cats}\n\nText: {text}\n\nRespond with only the category name, nothing else."
    result = generate(prompt, max_tokens=20)
    # Find best match
    result_lower = result.strip().lower()
    for cat in categories:
        if cat.lower() in result_lower:
            return cat
    return categories[0]


def extract_facts(text):
    """Extract structured facts from text using local model."""
    system = "You are a fact extractor. Given text, extract key facts as a JSON array of objects with 'fact', 'type' (commitment/decision/preference/relationship/event), and 'confidence' (0-1). Return only valid JSON."
    prompt = f"Extract facts from:\n\n{text}"
    result = generate(prompt, system=system, max_tokens=1000)
    try:
        # Try to parse JSON from the response
        start = result.find("[")
        end = result.rfind("]") + 1
        if start >= 0 and end > start:
            return json.loads(result[start:end])
    except json.JSONDecodeError:
        pass
    return []


if __name__ == "__main__":
    print("Testing local model...")
    print(f"Generate: {generate('What is 2+2? Answer briefly.')}")
    print(f"Classify: {classify('I need to send that report by Friday', ['commitment', 'question', 'observation', 'goal'])}")
    
    emb = embed("Hello world")
    print(f"Embedding: {len(emb[0])} dimensions")
    
    facts = extract_facts("I told <contact-name-b> I'd send the proposal by Friday. My goal is to exercise 4 times a week. <contact-name-d> recommended the new Thai place on Main Street.")
    print(f"Facts: {json.dumps(facts, indent=2)}")
