"""Create an isolated, explicitly synthetic ledger for UI development. Never touches var/."""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from fincl.db import connect_sqlite
from fincl.decision_journal import append_decision, default_journal_path
from fincl.document_service import DocumentImportService
from fincl.refresh import refresh_ledger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from mari_card_pdf import conversion_statement, mari_card_pdf_bytes  # noqa: E402
from revolut_pdf import revolut_pdf_bytes  # noqa: E402


def create_demo(root: Path) -> Path:
    root.mkdir(parents=True,exist_ok=True)
    database=root/'demo.sqlite3'
    if database.exists():
        raise RuntimeError('Choose an empty directory for the demo ledger')
    events=[]
    for month in range(3,9):
        events.append((date(2026,month,1),'Topup','Employer salary',Decimal('5000.00'),'Income & Inflow / Earned Income'))
        for day,merchant,amount,category in [(2,'Apartment rent','-1500.00','Housing / Rent'),(4,'Acme Streaming','-10.00','Leisure / Entertainment'),(7,'Corner Grocer','-90.00','Food & Dining / Groceries'),(10,'Mobile Plan','-30.00','Housing / Internet & Mobile'),(13,'Fresh Market','-50.00','Food & Dining / Groceries'),(15,'Lunch Counter','-15.00','Food & Dining / Dining Out'),(18,'Clothing Store','-80.00','Shopping / Clothing & Accessories'),(20,'Ride-hail','-20.00','Transport / Taxi & Ride-hail'),(22,'Neighbourhood Bakery','-15.00','Food & Dining / Cafes & Drinks'),(24,'Corner Grocer','-65.00','Food & Dining / Groceries'),(26,'Pharmacy','-25.00','Health / Pharmacy'),(28,'Transit Top-up','-40.00','Transport / Public Transit')]:
            value=Decimal(amount)
            if merchant in {'Corner Grocer','Clothing Store'}:value-=Decimal(month*3)
            events.append((date(2026,month,day),'Card Payment',merchant,value,category))
    events.append((date(2026,8,31),'Card Payment','Neighbourhood Cafe',Decimal('-5.00'),'Food & Dining / Cafes & Drinks'))
    rows=[];balance=Decimal(0);category_map={}
    for dt,kind,name,value,cat in sorted(events):
        timestamp=f'{dt.isoformat()} 10:00:00';balance+=value
        rows.append([kind,'Current',timestamp,timestamp,name,str(value),'0.00','SGD','COMPLETED',str(balance)])
        category_map[name]=cat
    source=root/'synthetic-revolut.pdf';source.write_bytes(revolut_pdf_bytes(rows))
    service=DocumentImportService.plaintext_phase2(database,root/'evidence')
    service.import_revolut_pdf(source)
    # A synthetic Mari credit-card statement, including an installment chain to confirm.
    service.import_mari_card(Path(__file__).resolve().parents[1]/'tests'/'fixtures'/'mari_card.pdf')
    # The next month: purchases converted to a new split payment, whose printed conversion credit is its principal.
    card_aug=root/'synthetic-mari-card-aug.pdf';card_aug.write_bytes(mari_card_pdf_bytes(conversion_statement()))
    service.import_mari_card(card_aug)
    c=connect_sqlite(database)
    entries=c.execute('SELECT e.natural_anchor,e.economic_direction,d.description_raw FROM ledger_entries e JOIN document_records d ON d.id=e.document_record_id').fetchall();c.close()
    for entry in entries:
        if entry['description_raw'] not in category_map:continue  # card lines stay unreviewed
        append_decision(default_journal_path(database),decision_kind='CATEGORY_OVERRIDE',anchors={'entry':entry['natural_anchor']},payload={'category':category_map[entry['description_raw']],'reason':'Synthetic demo classification'},policy_version='demo-v1',snapshot='Synthetic demo category')
        if entry['economic_direction']=='IN':
            append_decision(default_journal_path(database),decision_kind='INBOUND_CLASSIFICATION',anchors={'entry':entry['natural_anchor']},payload={'classification':'income','reason':'Synthetic demo salary'},policy_version='demo-v1',snapshot='Synthetic demo income')
    for ident,payload in [('groceries',{'type':'budget','name':'Groceries, with room to spare','currency':'SGD','target':'300.00','saved':'0','category':'Food & Dining / Groceries','reason':'Synthetic demo budget'}),('emergency',{'type':'goal','name':'A little peace of mind','currency':'SGD','target':'15000.00','saved':'6200.00','category':'','reason':'Synthetic demo goal'})]:
        append_decision(default_journal_path(database),decision_kind='PLAN_SET',anchors={'plan':ident},payload=payload,policy_version='demo-v1',snapshot='Synthetic demo plan')
    refresh_ledger(database)
    return database

if __name__=='__main__':
    print(create_demo(Path(sys.argv[1])))
