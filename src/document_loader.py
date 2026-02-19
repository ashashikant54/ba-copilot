# document_loader.py
# This file reads text from .txt, .docx (Word) and .pdf files
# It returns the text as a single string we can work with

import os


def load_txt(file_path):
    """Read a plain text file"""
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    return text


def load_docx(file_path):
    """Read a Word document (.docx)"""
    from docx import Document
    doc = Document(file_path)
    
    # Each paragraph in Word is a separate item - we join them all
    paragraphs = []
    for paragraph in doc.paragraphs:
        if paragraph.text.strip():  # Skip empty lines
            paragraphs.append(paragraph.text)
    
    return "\n".join(paragraphs)


def load_pdf(file_path):
    """Read a PDF file"""
    from pypdf import PdfReader
    reader = PdfReader(file_path)
    
    # PDFs have multiple pages - we read all of them
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text)
    
    return "\n".join(pages)


def load_document(file_path):
    """
    Main function - detects file type automatically
    and calls the right loader
    """
    # Check the file actually exists
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Cannot find file: {file_path}")
    
    # Get the file extension (.txt, .docx, .pdf)
    extension = os.path.splitext(file_path)[1].lower()
    
    if extension == ".txt":
        text = load_txt(file_path)
    elif extension == ".docx":
        text = load_docx(file_path)
    elif extension == ".pdf":
        text = load_pdf(file_path)
    else:
        raise ValueError(f"Sorry, I can only read .txt, .docx and .pdf files. You gave me: {extension}")
    
    print(f"✅ Loaded: {file_path}")
    print(f"📄 Characters read: {len(text)}")
    print(f"📝 Words read: {len(text.split())}")
    
    return text


# ── TEST ──────────────────────────────────────────────────────
# This block only runs when you run THIS file directly
# It won't run when other files import from this file

if __name__ == "__main__":
    print("Testing document loader...\n")
    
    # Test with our sample file
    file = "documents/sample_requirements.txt"
    
    text = load_document(file)
    
    print("\n── First 300 characters of the document ──")
    print(text[:300])
    print("...")
    print("\n✅ Document loader is working!")