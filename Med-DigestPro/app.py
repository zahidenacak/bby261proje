import os
import requests
import feedparser
from datetime import datetime
from flask import Flask, render_template, request, make_response, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select
import google.generativeai as genai
from dotenv import load_dotenv
from fpdf import FPDF
from bs4 import BeautifulSoup

# 1. AYARLAR
load_dotenv()
app = Flask(__name__)

# VERİTABANI AYARLARI
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'med_digest.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# 2. AI MODEL SEÇİMİ
api_key = os.getenv("GOOGLE_API_KEY")
model = None
selected_model_name = "Bulunamadı"

try:
    if api_key:
        genai.configure(api_key=api_key)
        available_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                available_models.append(m.name)
        
        if 'models/gemini-1.5-flash' in available_models:
            selected_model_name = 'gemini-1.5-flash'
        elif 'models/gemini-pro' in available_models:
            selected_model_name = 'gemini-pro'
        elif available_models:
            selected_model_name = available_models[0]
        
        if selected_model_name != "Bulunamadı":
            model = genai.GenerativeModel(selected_model_name)
            print(f"✅ BAŞARILI: {selected_model_name} modeli aktif edildi.")
        else:
            print("⚠️ UYARI: Uygun Gemini modeli bulunamadı.")
            
except Exception as e:
    print(f"❌ MODEL HATASI: {e}")

# 3. VERİTABANI MODELİ
class SearchLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    query = db.Column(db.String(255), nullable=False)
    summary = db.Column(db.Text, nullable=False)
    persona = db.Column(db.String(50), default="Doktor")
    date = db.Column(db.DateTime, default=datetime.utcnow)
    sources = db.Column(db.Text, nullable=True)

with app.app_context():
    db.create_all()

# 4. YARDIMCI FONKSİYONLAR
def search_pubmed(query, start_year=None, end_year=None):
    base_query = query
    if start_year and end_year:
        base_query += f" AND {start_year}:{end_year}[pdat]"
    elif start_year:
        base_query += f" AND {start_year}:3000[pdat]"
        
    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    try:
        params = {'db': 'pubmed', 'term': base_query, 'retmode': 'json', 'retmax': '5', 'sort': 'relevance'}
        resp = requests.get(search_url, params=params)
        id_list = resp.json().get('esearchresult', {}).get('idlist', [])
        
        if not id_list: return [], "Kriterlere uygun makale bulunamadı."
            
        fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        fetch_resp = requests.get(fetch_url, params={'db': 'pubmed', 'id': ",".join(id_list), 'retmode': 'xml'})
        soup = BeautifulSoup(fetch_resp.content, 'xml')
        
        articles = []
        for art in soup.find_all('PubmedArticle'):
            title = art.find('ArticleTitle').text
            abstract_texts = art.find_all('AbstractText')
            abstract = " ".join([t.text for t in abstract_texts]) if abstract_texts else "Özet bulunamadı."
            pmid = art.find('PMID').text
            pub_date = art.find('PubDate')
            year_text = pub_date.find('Year').text if pub_date and pub_date.find('Year') else "????"
            
            articles.append({
                'title': title, 
                'abstract': abstract, 
                'year': year_text, 
                'link': f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            })
        return articles, None
    except Exception as e:
        return [], f"Veri Kaynağı Hatası: {str(e)}"

def generate_ai_summary(query, articles, persona="Doktor"):
    if not model: return f"HATA: AI Modeli başlatılamadı ({selected_model_name})."
    
    context = "\n".join([f"- {art['title']}: {art['abstract'][:500]}" for art in articles])
    instruction = "Hastalar için çok sade ve anlaşılır dille" if persona == "Hasta" else "Doktorlar için tıbbi terminolojiyle"
    
    prompt = (
        f"Sen bir tıbbi asistansın. Konu: '{query}'. Hedef Kitle: {persona}. ({instruction}). "
        f"Aşağıdaki makale özetlerini kullanarak kapsamlı bir sentez raporu oluştur. "
        f"Lütfen yanıtını şu formatta ver:\n"
        f"1. En başa 'GENEL SONUÇ' adında kısa bir paragraf yaz.\n"
        f"2. Sonraki detayları '### ' (üç kare) işaretiyle başlayan alt başlıklar halinde yaz. (Örn: '### Tedavi Yöntemleri', '### Yan Etkiler'). "
        f"Bu başlıkların altına detaylı maddeler ekle.\n"
        f"Makaleler:\n{context}"
    )
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"AI Yanıt Üretemedi: {str(e)}"

def get_nyt_health_news():
    try:
        feed = feedparser.parse("https://rss.nytimes.com/services/xml/rss/nyt/Health.xml")
        news = [{'title': e.title, 'link': e.link, 'published': e.published[:16]} for e in feed.entries[:5]]
        return news
    except:
        return []

class PDFReport(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 12)
        self.cell(0, 10, 'Med-Digest AI Raporu', 0, 1, 'C')
        self.ln(10)

# 5. ROTALAR
@app.route('/', methods=['GET', 'POST'])
def index():
    result, error, articles = None, None, []
    query = ""
    start_year, end_year = "", ""
    persona = "Doktor"
    
    if request.method == 'POST':
        query = request.form.get('query')
        start_year = request.form.get('start_year')
        end_year = request.form.get('end_year')
        persona = request.form.get('persona')
        
        if query:
            articles, error = search_pubmed(query, start_year, end_year)
            if not error:
                result = generate_ai_summary(query, articles, persona)
                try:
                    source_str = " || ".join([a['title'] for a in articles])
                    new_log = SearchLog(query=query[:250], summary=result, persona=persona, sources=source_str)
                    db.session.add(new_log)
                    db.session.commit()
                    print(f"✅ Kayıt Başarılı: {query}")
                except Exception as e: 
                    print(f"❌ KAYIT HATASI: {e}")
                    db.session.rollback()

    try:
        stmt = select(SearchLog).order_by(SearchLog.date.desc()).limit(5)
        history = db.session.execute(stmt).scalars().all()
    except: history = []

    news_list = get_nyt_health_news()
    
    return render_template('index.html', result=result, error=error, articles=articles, 
                           query=query, history=history, news_list=news_list, 
                           start_year=start_year, end_year=end_year, persona=persona,
                           active_model=selected_model_name)

@app.route('/analyze_article', methods=['POST'])
def analyze_article():
    data = request.json
    title = data.get('title')
    abstract = data.get('abstract')
    persona = data.get('persona', 'Doktor')
    
    if not model: return jsonify({'analysis': 'AI Modeli aktif değil.'})
    
    prompt = f"Şu makaleyi {persona} için Türkçe analiz et. Başlıkları ### ile ayır: {title}\n{abstract[:800]}"
    try:
        response = model.generate_content(prompt)
        return jsonify({'analysis': response.text})
    except Exception as e:
        return jsonify({'analysis': f"Hata: {str(e)}"})

@app.route('/download/<int:log_id>')
def download_pdf(log_id):
    log = db.session.get(SearchLog, log_id)
    if not log: return "Kayıt bulunamadı", 404
    
    pdf = PDFReport()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    
    def clean(text):
        return text.encode('latin-1', 'replace').decode('latin-1')

    pdf.cell(0, 10, clean(f"Konu: {log.query}"), ln=True)
    pdf.set_font("Arial", 'I', 10)
    pdf.cell(0, 10, clean(f"Hedef: {log.persona} | Tarih: {log.date.strftime('%Y-%m-%d')}"), ln=True)
    pdf.ln(5)
    pdf.set_font("Arial", '', 11)
    
    clean_summary = log.summary.replace('### ', '').replace('**', '')
    pdf.multi_cell(0, 7, clean(clean_summary))
    
    pdf_content = pdf.output(dest='S').encode('latin-1', 'replace')
    response = make_response(pdf_content)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename=report_{log.id}.pdf'
    return response

if __name__ == '__main__':
    app.run(debug=True)