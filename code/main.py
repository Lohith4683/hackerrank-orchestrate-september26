import csv,re,os
from pathlib import Path
from datetime import date,timedelta
from decimal import Decimal,ROUND_HALF_UP
from collections import defaultdict,Counter
BASE=Path(__file__).resolve().parent.parent; DATA=BASE/'dataset'; MEDIA=DATA/'media'/'images'
def rows(n):
 with open(DATA/n,encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
R=rows('requests.csv'); P={x['user_id']:x for x in rows('financial_profiles.csv')}; E=rows('financial_events.csv'); O=rows('request_payment_options.csv'); M=rows('messages.csv'); I=rows('images.csv'); X=rows('exchange_rates.csv')
RM={(x['rate_date'],x['from_currency'],x['to_currency']):Decimal(x['rate']) for x in X}
IMG={x['related_event_id']:MEDIA/(x['image_id']+'.png') for x in I}
try:
 import pytesseract
 from PIL import Image
 pytesseract.pytesseract.tesseract_cmd=r'C:\Program Files\Tesseract-OCR\tesseract.exe'
except: pytesseract=None

def dec(s):
 try:return Decimal(str(s).replace(',','').strip())
 except:return None
def D(s):return date.fromisoformat(s)
def money_text(t):
 z=re.findall(r'(?<![A-Za-z0-9])(?:IDR|INR|ZAR|USD|EUR|\$|₹|€)?\s*(?:\d{1,3}(?:,\d{2})+,\d{3}|\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?!\d)',t,re.I)
 return [dec(re.sub(r'[^0-9.]','',v)) for v in z if re.search(r'\d',v)]
OCR={}
def imgamt(eid):
 if eid in OCR:return OCR[eid]
 p=IMG.get(eid)
 if not p or not pytesseract:return None
 try:
  im=Image.open(p).convert('L'); vals=[]
  for psm in (6,11):
   t=pytesseract.image_to_string(im.resize((im.width*3,im.height*3)),config=f'--psm {psm}')
   # prioritize labeled totals/due/paid/net
   for pat in [r'(?:net amount|total amount|amount due|amount paid|cash paid|grand total|total)\s*[:=-]?\s*[^\d]{0,8}([\d,]+(?:\.\d{1,2})?)']:
    for q in re.findall(pat,t,re.I):
     v=dec(q)
     if v is not None: vals.append(v)
  if vals: OCR[eid]=max(vals); return OCR[eid]
 except: pass
 return None

def amt(e):
 v=dec(e.get('amount',''))
 return v if v is not None else imgamt(e['event_id'])
def rate(dt,a,b):
 if a==b:return Decimal(1)
 for k in ((dt,a,b),(dt,b,a)):
  if k in RM:return RM[k] if a==k[1] else Decimal(1)/RM[k]
 return Decimal(1)

def event_amt(e,cur,dt):
 v=amt(e)
 return None if v is None else v*rate(dt,e['currency'],cur)

def flexible(e,prof):
 cat=e.get('category',''); f=e.get('flexibility','').lower()
 if e.get('direction')!='debit' or e.get('status')!='settled':return False
 if f not in ('flexible','stoppable','reducible','reducible_or_stoppable'):return False
 return cat in prof.get('expense_categories_user_willing_to_reduce','') or cat in prof.get('expense_categories_user_willing_to_stop','')

def recurring(ev):
 g=defaultdict(list)
 for e in ev:
  if e['status']=='settled' and e['direction']=='debit' and e['event_type'] in ('subscription','expense','debt_payment') and dec(e.get('amount')) is not None:g[(e['event_type'],e['category'],e['currency'],e['description'])].append(e)
 out=[]
 for k,ls in g.items():
  ds=sorted(D(x['event_date']) for x in ls)
  if len(ds)<3:continue
  gaps=[(ds[i]-ds[i-1]).days for i in range(1,len(ds))]
  if 20<=sum(gaps)/len(gaps)<=40 and max(gaps)-min(gaps)<=12:out.append((k,ls,round(sum(gaps)/len(gaps))))
 return out
REC=recurring(E)

def fmt(v):
 return str(v.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)).rstrip('0').rstrip('.')
def planstr(ps):return '|'.join(f'{d.isoformat()}:{fmt(v)}' for d,v in ps)
def msgs(rid,uid):return [m for m in M if m['request_id']==rid or (m['user_id']==uid and m['related_event_id'])]
def opts(rid):return sorted([o for o in O if o['request_id']==rid],key=lambda x:int(x['payment_option_id'].split('_')[-1]) if x['payment_option_id'].split('_')[-1].isdigit() else 0)

def solve(r):
 uid=r['user_id']; prof=P[uid]; cur=prof['home_currency']; rd=D(r['request_date']); req=dec(r['requested_amount']); mind=dec(prof['minimum_balance_to_keep']) or Decimal(0); bal=dec(prof['current_available_balance']) or Decimal(0); deadline=D(r['desired_completion_date'])
 ev=[e for e in E if e['user_id']==uid]
 # replace blank image amounts; settled cashflow before request and future confirmed events
 safe=bal-mind
 for e in ev:
  ed=D(e['event_date']); sd=D(e['settlement_date']) if e.get('settlement_date') else ed
  v=event_amt(e,cur,sd)
  if v is None:continue
  if e['status']=='settled' and sd<=rd:
   safe += v if e['direction']=='credit' else -v
 # historical balance field is generally already current; avoid double-counting if event history appears cumulative
 safe=max(Decimal(0),safe)
 # future cashflow simulation from request date, with recurring projection from last observed pattern
 future=defaultdict(Decimal)
 for e in ev:
  sd=D(e['settlement_date']) if e.get('settlement_date') else D(e['event_date'])
  if rd<sd<=rd+timedelta(days=90) and e['status'] in ('scheduled','settled'):
   v=event_amt(e,cur,sd)
   if v is not None:future[sd]+=v if e['direction']=='credit' else -v
 for k,ls,gap in REC:
  if not ls or ls[0]['user_id']!=uid:continue
  last=max(ls,key=lambda x:D(x['event_date'])); d=D(last['event_date'])+timedelta(days=gap); v=event_amt(last,cur,D(last['event_date']))
  while d<=rd+timedelta(days=90):
   if d>rd and v is not None:future[d]+= -v
   d+=timedelta(days=gap)
 # salary/income recurring too
 inc=[e for e in ev if e['direction']=='credit' and e['status']=='settled' and dec(e.get('amount')) is not None]
 if len(inc)>=3:
  ds=sorted(D(e['event_date']) for e in inc[-8:]); gaps=[(ds[i]-ds[i-1]).days for i in range(1,len(ds))]
  g=round(sum(gaps)/len(gaps)) if gaps else 30
  if 20<=g<=40:
   last=max(inc,key=lambda x:D(x['event_date'])); d=D(last['event_date'])+timedelta(days=g);v=event_amt(last,cur,D(last['event_date']))
   while d<=rd+timedelta(days=90):
    if d>rd and v is not None:future[d]+=v
    d+=timedelta(days=g)
 def balance_after(extra=Decimal(0),changes=set(),until=deadline):
  b=bal-safe+safe-extra # simplifies to bal-extra; historical safe used below separately
  # use current balance baseline for forward safety
  b=bal
  for d in sorted(k for k in future if k<=until):
   b+=future[d]
   if d==rd:b-=extra
   for eid in changes:
    pass
   if b<mind:return False
  return True
 # safe today based on current balance, not future
 safe_today=max(Decimal(0),bal-mind)
 # find earliest full-payment date under 90d
 earliest=None; b=bal
 for i in range(91):
  d=rd+timedelta(days=i)
  b+=future.get(d,0)
  if b-req>=mind:
   earliest=d;break
 # optional spending changes: identify largest flexible recurring debit due in horizon and construct stop/reduce
 changes=[]
 if safe_today<req:
  cand=[]
  for k,ls,gap in REC:
   e=max(ls,key=lambda x:D(x['event_date']))
   if e['user_id']==uid and flexible(e,prof):
    v=event_amt(e,cur,D(e['event_date']))
    if v: cand.append((v,e))
  for v,e in sorted(cand,reverse=True,key=lambda x:x[0]):
   f=e.get('flexibility','').lower(); cat=e.get('category','')
   if f in ('stoppable','reducible_or_stoppable') and cat in prof.get('expense_categories_user_willing_to_stop',''):
    changes.append((f'stop:{e["event_id"]}',v));break
   if f in ('reducible','reducible_or_stoppable') and cat in prof.get('expense_categories_user_willing_to_reduce',''):
    mn=dec(e.get('minimum_allowed_amount')) or Decimal(0); nv=min(v,max(mn,Decimal(0))); changes.append((f'reduce_to:{e["event_id"]}:{fmt(nv)}',v-nv));break
 # message constraints
 text=' '.join(m['message_text'] for m in msgs(r['request_id'],uid)).lower()
 accept_full='full_payment' in text or 'pay in full' in text or 'full' in text or not text
 accept_inst='installment' in text or 'installments' in text or 'monthly' in text
 accept_partial='partial' in text
 # payment option feasibility
 best=None
 for o in opts(r['request_id']):
  method=o['payment_method']; n=int(o['number_of_payments']); first=D(o['first_payment_date']); freq=int(o['payment_frequency_days'] or 0); total=dec(o['total_payable_amount']) or req
  vals=[dec(o['payment_amount'])]*n; dates=[first+timedelta(days=freq*j) for j in range(n)]
  if dates[-1]>deadline or method not in ('installments','full_payment','partial_payment'):continue
  if method=='installments' and not accept_inst and text:continue
  ok=True;b=bal
  for d,v in zip(dates,vals):
   if d<rd:continue
   for fd in sorted(k for k in future if k<=d):
    if fd>=rd:b+=future[fd]; future[fd]=Decimal(0)
   b-=v
   if b<mind:ok=False;break
  if ok and total>=req:best=(o,dates,vals,total);break
 # immediate full payment, with optional change
 chsum=sum(x[1] for x in changes)
 if safe_today>=req and (accept_full or not text):
  return r['request_id'],fmt(req),'affordable_now','full_payment',f'{rd.isoformat()}:{fmt(req)}',rd.isoformat(),'none','The requested amount can be paid now while keeping the required minimum balance.'
 if safe_today+chsum>=req and changes and (accept_full or not text):
  cs='|'.join(x[0] for x in changes[:3]);return r['request_id'],fmt(safe_today),'affordable_with_plan','full_payment',f'{rd.isoformat()}:{fmt(req)}',rd.isoformat(),cs,'The payment is safe after applying flexible spending changes while preserving the minimum balance.'
 if r['allows_partial_payment'].lower()=='true' and accept_partial and safe_today>0 and safe_today<req and earliest and earliest<=deadline:
  rem=req-safe_today;return r['request_id'],fmt(safe_today),'affordable_with_plan','partial_payment',f'{rd.isoformat()}:{fmt(safe_today)}|{earliest.isoformat()}:{fmt(rem)}',earliest.isoformat(),'none','A partial payment can be made now and the remaining amount can be paid when the full amount is safe.'
 if best:
  o,dates,vals,total=best; ps=planstr(list(zip(dates,vals))); er=earliest.isoformat() if earliest else ''
  return r['request_id'],fmt(safe_today),'affordable_with_plan','installments',ps,er,'none','The available installment option completes the purchase by the requested deadline without breaching the minimum balance.'
 if earliest and earliest<=deadline and (accept_full or not text):
  return r['request_id'],fmt(safe_today),'affordable_later','wait',f'{earliest.isoformat()}:{fmt(req)}',earliest.isoformat(),'none','Waiting until the earliest safe date avoids dropping below the required minimum balance.'
 return r['request_id'],fmt(safe_today),'not_affordable','not_recommended','none','', 'none','The requested payment cannot be made safely within the available forecast and deadline.'

out=[]
for r in R:
 try:out.append(solve(r))
 except Exception as e:out.append((r['request_id'],'0','not_affordable','not_recommended','none','','none','Unable to establish a safe payment plan from the available financial data.'))
cols=['request_id','amount_safe_to_pay','affordability_status','recommended_payment_method','payment_plan','earliest_date_for_full_payment','spending_changes_needed','decision_explanation']
with open(BASE/'output.csv','w',encoding='utf-8',newline='') as f:
 w=csv.writer(f);w.writerow(cols);w.writerows(out)
print('Generated output.csv:',len(out),'rows')
