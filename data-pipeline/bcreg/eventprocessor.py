#!/usr/bin/python
 
import psycopg2
import datetime
import pytz
import json
import time
import hashlib
import traceback
from bcreg.config import config
from bcreg.bcregistries import BCRegistries, CustomJsonEncoder, event_dict, is_data_conversion_event


corp_credential = 'REG'
corp_schema = 'registration.registries.ca'
corp_version = '1.0.41'

addr_credential = 'ADDR'
addr_schema = 'address.registries.ca'
addr_version = '1.0.41'

dba_credential = 'REL'
dba_schema = 'relationship.registries.ca'
dba_version = '1.0.41'

CORP_BATCH_SIZE = 3000
FALLBACK_CORP_BATCH_SIZE = 300

MIN_START_DATE = datetime.datetime(datetime.MINYEAR+1, 1, 1)
MAX_END_DATE   = datetime.datetime(datetime.MAXYEAR-1, 12, 31)

# for now, we are in PST time
timezone = pytz.timezone("America/Los_Angeles")

MIN_START_DATE_TZ = timezone.localize(MIN_START_DATE)
MAX_END_DATE_TZ   = timezone.localize(MAX_END_DATE)


class DateTimeEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime.datetime):
            try:
                tz_aware = timezone.localize(o)
                return tz_aware.astimezone(pytz.utc).isoformat()
            except (Exception) as error:
                if o.year <= datetime.MINYEAR+1:
                    return MIN_START_DATE_TZ.astimezone(pytz.utc).isoformat()
                elif o.year >= datetime.MAXYEAR-1:
                    return MAX_END_DATE_TZ.astimezone(pytz.utc).isoformat()
                return o.isoformat()
        return json.JSONEncoder.default(self, o)


def event_json(event):
    return json.dumps(event, cls=CustomJsonEncoder, sort_keys=True)

def event_dict_from_json(event_json):
    return json.loads(event_json)


# interface to Event Processor database
class EventProcessor:
    def __init__(self):
        try:
            params = config(section='event_processor')
            self.conn = psycopg2.connect(**params)
        except (Exception) as error:
            print(error)
            print(traceback.print_exc())
            self.conn = None
            raise

    def __del__(self):
        if self.conn:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass
 
    # create our base processing tables
    def create_tables(self):
        """ create tables in the PostgreSQL database"""
        commands = (
            """
            CREATE TABLE IF NOT EXISTS LAST_EVENT (
                RECORD_ID SERIAL PRIMARY KEY,
                SYSTEM_TYPE_CD VARCHAR(255) NOT NULL, 
                EVENT_ID INTEGER NOT NULL,
                EVENT_DATE TIMESTAMP NOT NULL,
                ENTRY_DATE TIMESTAMP NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS le_stc ON LAST_EVENT 
            (SYSTEM_TYPE_CD);
            """,
            """
            CREATE INDEX IF NOT EXISTS le_ie ON LAST_EVENT 
            (EVENT_ID);
            """,
            """
            CREATE INDEX IF NOT EXISTS le_stc_ei ON LAST_EVENT 
            (SYSTEM_TYPE_CD, EVENT_ID);
            """,
            """
            CREATE TABLE IF NOT EXISTS EVENT_BY_CORP_FILING (
                RECORD_ID SERIAL PRIMARY KEY,
                SYSTEM_TYPE_CD VARCHAR(255) NOT NULL, 
                CORP_NUM VARCHAR(255) NOT NULL,
                PREV_EVENT_ID INTEGER NOT NULL, 
                PREV_EVENT_DATE TIMESTAMP NOT NULL, 
                LAST_EVENT_ID INTEGER NOT NULL, 
                LAST_EVENT_DATE TIMESTAMP NOT NULL, 
                ENTRY_DATE TIMESTAMP NOT NULL,
                PROCESS_DATE TIMESTAMP,
                PROCESS_SUCCESS CHAR,
                PROCESS_MSG VARCHAR(255)
            )
            """,
            """
            -- Hit for counts and queries
            CREATE INDEX IF NOT EXISTS ebcf_pd_null ON EVENT_BY_CORP_FILING 
            (PROCESS_DATE) WHERE PROCESS_DATE IS NULL;
            """,
            """
            -- Hit for query
            CREATE INDEX IF NOT EXISTS ebcf_ri_pd_null_asc ON EVENT_BY_CORP_FILING 
            (RECORD_ID ASC, PROCESS_DATE) WHERE PROCESS_DATE IS NULL;
            """,
            """
            ALTER TABLE EVENT_BY_CORP_FILING
            SET (autovacuum_vacuum_scale_factor = 0.0);
            """,
            """ 
            ALTER TABLE EVENT_BY_CORP_FILING
            SET (autovacuum_vacuum_threshold = 5000);
            """,
            """
            ALTER TABLE EVENT_BY_CORP_FILING  
            SET (autovacuum_analyze_scale_factor = 0.0);
            """,
            """ 
            ALTER TABLE EVENT_BY_CORP_FILING  
            SET (autovacuum_analyze_threshold = 5000);
            """,
            """ 
            REINDEX TABLE EVENT_BY_CORP_FILING;
            """,
            """
            CREATE TABLE IF NOT EXISTS CORP_HISTORY_LOG (
                RECORD_ID SERIAL PRIMARY KEY,
                SYSTEM_TYPE_CD VARCHAR(255) NOT NULL, 
                CORP_NUM VARCHAR(255) NOT NULL,
                CORP_STATE VARCHAR(255) NOT NULL,
                PREV_EVENT JSON NOT NULL, 
                LAST_EVENT JSON NOT NULL, 
                CORP_JSON JSON NOT NULL,
                ENTRY_DATE TIMESTAMP NOT NULL,
                PROCESS_DATE TIMESTAMP,
                PROCESS_SUCCESS CHAR,
                PROCESS_MSG VARCHAR(255)
            )
            """,
            """
            -- Hit for counts and queries
            CREATE INDEX IF NOT EXISTS chl_pd_null ON CORP_HISTORY_LOG 
            (PROCESS_DATE) WHERE PROCESS_DATE IS NULL;
            """,
            """
            -- Hit for query
            CREATE INDEX IF NOT EXISTS chl_ri_pd_null_asc ON CORP_HISTORY_LOG 
            (RECORD_ID ASC, PROCESS_DATE) WHERE PROCESS_DATE IS NULL;	
            """,
            """
            ALTER TABLE CORP_HISTORY_LOG
            SET (autovacuum_vacuum_scale_factor = 0.0);
            """,
            """ 
            ALTER TABLE CORP_HISTORY_LOG
            SET (autovacuum_vacuum_threshold = 5000);
            """,
            """
            ALTER TABLE CORP_HISTORY_LOG  
            SET (autovacuum_analyze_scale_factor = 0.0);
            """,
            """ 
            ALTER TABLE CORP_HISTORY_LOG  
            SET (autovacuum_analyze_threshold = 5000);
            """,
            """ 
            REINDEX TABLE CORP_HISTORY_LOG;
            """,
            """
            CREATE TABLE IF NOT EXISTS CREDENTIAL_TRANSFORM (
                RECORD_ID SERIAL PRIMARY KEY,
                SYSTEM_TYPE_CD VARCHAR(255) NOT NULL, 
                CREDENTIAL_TYPE_CD VARCHAR(255) NOT NULL,
                SCHEMA_NAME VARCHAR(255) NOT NULL,
                SCHEMA_VERSION VARCHAR(255) NOT NULL,
                MAPPING_TRANSFORM TEXT NOT NULL
            )
            """,
            """
            -- Hit for query
            CREATE INDEX IF NOT EXISTS ct_stc ON CREDENTIAL_TRANSFORM 
            (SYSTEM_TYPE_CD);
            """,
            """
            CREATE TABLE IF NOT EXISTS CREDENTIAL_LOG (
                RECORD_ID SERIAL PRIMARY KEY,
                SYSTEM_TYPE_CD VARCHAR(255) NOT NULL, 
                CORP_NUM VARCHAR(255) NOT NULL,
                CORP_STATE VARCHAR(255) NOT NULL,
                PREV_EVENT JSON NOT NULL, 
                LAST_EVENT JSON NOT NULL, 
                CREDENTIAL_TYPE_CD VARCHAR(255) NOT NULL,
                CREDENTIAL_ID VARCHAR(255) NOT NULL,
                SCHEMA_NAME VARCHAR(255) NOT NULL,
                SCHEMA_VERSION VARCHAR(255) NOT NULL,
                CREDENTIAL_JSON JSON NOT NULL,
                CREDENTIAL_HASH VARCHAR(64) NOT NULL, 
                CREDENTIAL_REASON VARCHAR(255),
                ENTRY_DATE TIMESTAMP NOT NULL,
                END_DATE TIMESTAMP,
                PROCESS_DATE TIMESTAMP,
                PROCESS_SUCCESS CHAR,
                PROCESS_MSG VARCHAR(255)
            )
            """,
            """
            -- Hit duplicate credentials
            CREATE UNIQUE INDEX IF NOT EXISTS cl_hash_index ON CREDENTIAL_LOG 
            (CREDENTIAL_HASH);
            """,
            """
            -- Hit for counts and queries
            CREATE INDEX IF NOT EXISTS cl_pd_null ON CREDENTIAL_LOG 
            (PROCESS_DATE) WHERE PROCESS_DATE IS NULL;
            """,
            """
            -- Hit for counts
            CREATE INDEX IF NOT EXISTS cl_pd_null_cs_act ON CREDENTIAL_LOG 
            (PROCESS_DATE, CORP_STATE) WHERE CORP_STATE = 'ACT' and PROCESS_DATE IS NULL;
            """,
            """
            -- Hit for counts
            CREATE INDEX IF NOT EXISTS cl_cs_act_pd_null_ri_asc ON CREDENTIAL_LOG 
            (CORP_STATE, PROCESS_DATE, RECORD_ID ASC) WHERE CORP_STATE = 'ACT' and PROCESS_DATE IS NULL;
            """,
            """
            -- Hit for queries
            CREATE INDEX IF NOT EXISTS cl_ri_cs_act_pd_null_asc ON CREDENTIAL_LOG 
            (RECORD_ID ASC, CORP_STATE, PROCESS_DATE) WHERE CORP_STATE = 'ACT' and PROCESS_DATE IS NULL;
            """,
            """
            -- Hit for query
            CREATE INDEX IF NOT EXISTS cl_ri_pd_null_asc ON CREDENTIAL_LOG 
            (RECORD_ID ASC, PROCESS_DATE) WHERE PROCESS_DATE IS NULL;
            """,
            #"""
            #-- Hit when checking generated credentials
            #CREATE INDEX IF NOT EXISTS cl_ri_stc_cn_cs_ctc_ci_desc ON CREDENTIAL_LOG 
            #(RECORD_ID DESC,SYSTEM_TYPE_CD, CORP_NUM, CORP_STATE, CREDENTIAL_TYPE_CD, CREDENTIAL_ID)
            #""",
            """
            -- Hit for counts
            CREATE INDEX IF NOT EXISTS cl_ps ON CREDENTIAL_LOG
            (process_success)
            """,
            """
            -- Hit for queries
            CREATE INDEX IF NOT EXISTS cl_ps_pd_desc ON CREDENTIAL_LOG
            (process_success, process_date DESC)
            """,
            """
            ALTER TABLE CREDENTIAL_LOG
            SET (autovacuum_vacuum_scale_factor = 0.0);
            """,
            """ 
            ALTER TABLE CREDENTIAL_LOG
            SET (autovacuum_vacuum_threshold = 5000);
            """,
            """
            ALTER TABLE CREDENTIAL_LOG  
            SET (autovacuum_analyze_scale_factor = 0.0);
            """,
            """ 
            ALTER TABLE CREDENTIAL_LOG  
            SET (autovacuum_analyze_threshold = 5000);
            """,
            """ 
            REINDEX TABLE CREDENTIAL_LOG;
            """
            )
        cur = None
        try:
            cur = self.conn.cursor()
            for command in commands:
                cur.execute(command)
            self.conn.commit()
            cur.close()
            cur = None
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            print(traceback.print_exc())
            raise
        finally:
            if cur is not None:
                cur.close()

    # record the last event processed
    def insert_last_event(self, system_type, event_id, event_date):
        """ insert a new event into the event table """
        sql = """INSERT INTO LAST_EVENT (SYSTEM_TYPE_CD, EVENT_ID, EVENT_DATE, ENTRY_DATE)
                 VALUES(%s, %s, %s, %s) RETURNING RECORD_ID;"""
        cur = None
        try:
            cur = self.conn.cursor()
            cur.execute(sql, (system_type, event_id, event_date, datetime.datetime.now(),))
            _record_id = cur.fetchone()[0]
            self.conn.commit()
            cur.close()
            cur = None
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            print(traceback.print_exc())
            raise
        finally:
            if cur is not None:
                cur.close()

    # get the id of the last event processed (at a specific date)
    def get_last_processed_event(self, event_date, system_type):
        cur = None
        try:
            cur = self.conn.cursor()
            cur.execute("""SELECT max(event_id) FROM LAST_EVENT where EVENT_DATE = %s and SYSTEM_TYPE_CD = %s""", (event_date, system_type,))
            row = cur.fetchone()
            cur.close()
            cur = None
            prev_event = row[0]
            if prev_event is None:
                prev_event = 0
            return prev_event
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            print(traceback.print_exc())
            raise
        finally:
            if cur is not None:
                cur.close()

    # get the last event processed timestamp
    def get_last_processed_event_date(self, system_type):
        cur = None
        try:
            cur = self.conn.cursor()
            cur.execute("""SELECT max(event_date) FROM LAST_EVENT where SYSTEM_TYPE_CD = %s""", (system_type,))
            row = cur.fetchone()
            cur.close()
            cur = None
            prev_event = row[0]
            return prev_event
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            print(traceback.print_exc())
            raise
        finally:
            if cur is not None:
                cur.close()

    # insert a record into the "unprocessed corporations" table
    def insert_corporation(self, system_type, prev_event_id, prev_event_dt, last_event_id, last_event_dt, corp_num):
        """ insert a new corps into the corps table """
        sql = """INSERT INTO EVENT_BY_CORP_FILING (SYSTEM_TYPE_CD, PREV_EVENT_ID, PREV_EVENT_DATE, LAST_EVENT_ID, LAST_EVENT_DATE, CORP_NUM, ENTRY_DATE)
                 VALUES(%s, %s, %s, %s, %s, %s, %s) RETURNING RECORD_ID;"""
        cur = None
        try:
            cur = self.conn.cursor()
            cur.execute(sql, (system_type, prev_event_id, prev_event_dt, last_event_id, last_event_dt, 
                                corp_num, datetime.datetime.now(),))
            _record_id = cur.fetchone()[0]
            self.conn.commit()
            cur.close()
            cur = None
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            print(traceback.print_exc())
            raise
        finally:
            if cur is not None:
                cur.close()

    # insert a list of "unprocessed corporations" into the table
    def insert_corporation_list(self, corporation_list):
        """ insert multiple corps into the corps table  """
        sql = """INSERT INTO EVENT_BY_CORP_FILING (SYSTEM_TYPE_CD, PREV_EVENT_ID, PREV_EVENT_DATE, LAST_EVENT_ID, LAST_EVENT_DATE, CORP_NUM, ENTRY_DATE) 
                 VALUES(%s, %s, %s, %s, %s)"""
        cur = None
        try:
            cur = self.conn.cursor()
            cur.executemany(sql, corporation_list)
            self.conn.commit()
            cur.close()
            cur = None
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            print(traceback.print_exc())
            raise
        finally:
            if cur is not None:
                cur.close()

    # update a group of corps into the "unprocessed corp" queue
    def update_corp_event_queue(self, system_type, corps, max_event_id, max_event_date):
        sql = """INSERT INTO EVENT_BY_CORP_FILING (SYSTEM_TYPE_CD, PREV_EVENT_ID, PREV_EVENT_DATE, LAST_EVENT_ID, LAST_EVENT_DATE, CORP_NUM, ENTRY_DATE)
                 VALUES(%s, %s, %s, %s, %s, %s, %s) RETURNING RECORD_ID;"""
        sql2 = """INSERT INTO LAST_EVENT (SYSTEM_TYPE_CD, EVENT_ID, EVENT_DATE, ENTRY_DATE)
                 VALUES(%s, %s, %s, %s) RETURNING RECORD_ID;"""
        cur = None
        try:
            for i,corp in enumerate(corps): 
                cur = self.conn.cursor()
                cur.execute(sql, (system_type, corp['PREV_EVENT']['event_id'], corp['PREV_EVENT']['event_date'], corp['LAST_EVENT']['event_id'], corp['LAST_EVENT']['event_date'], corp['CORP_NUM'], datetime.datetime.now(),))
                _record_id = cur.fetchone()[0]
                cur.close()
                cur = None
            cur = self.conn.cursor()
            cur.execute(sql2, (system_type, max_event_id, max_event_date, datetime.datetime.now(),))
            self.conn.commit()
            cur = None
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            print(traceback.print_exc())
            raise
        finally:
            if cur is not None:
                cur.close()

    # insert data for one corp into the history table
    def insert_corp_history(self, system_type, prev_event_json, last_event_json, corp_num, corp_state, corp_json):
        """ insert a new corps into the corps table """
        sql = """INSERT INTO CORP_HISTORY_LOG (SYSTEM_TYPE_CD, PREV_EVENT, LAST_EVENT, CORP_NUM, CORP_STATE, CORP_JSON, ENTRY_DATE)
                 VALUES(%s, %s, %s, %s, %s, %s, %s) RETURNING RECORD_ID;"""
        cur = None
        try:
            cur = self.conn.cursor()
            cur.execute(sql, (system_type, prev_event_json, last_event_json, corp_num, corp_state, corp_json, datetime.datetime.now(),))
            _record_id = cur.fetchone()[0]
            self.conn.commit()
            cur.close()
            cur = None
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            print(traceback.print_exc())
            raise
        finally:
            if cur is not None:
                cur.close()

    # insert a generated JSON credential into our log
    def insert_json_credential(self, cur, system_cd, prev_event, last_event, corp_num, corp_state, cred_type, cred_id, schema_name, schema_version, credential, credential_reason):
        sql = """INSERT INTO CREDENTIAL_LOG (SYSTEM_TYPE_CD, PREV_EVENT, LAST_EVENT, CORP_NUM, CORP_STATE, CREDENTIAL_TYPE_CD, CREDENTIAL_ID, 
                SCHEMA_NAME, SCHEMA_VERSION, CREDENTIAL_JSON, CREDENTIAL_HASH, CREDENTIAL_REASON, ENTRY_DATE)
                VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING RECORD_ID;"""
        sql_addr = """INSERT INTO CREDENTIAL_LOG (SYSTEM_TYPE_CD, PREV_EVENT, LAST_EVENT, CORP_NUM, CORP_STATE, CREDENTIAL_TYPE_CD, CREDENTIAL_ID, 
                SCHEMA_NAME, SCHEMA_VERSION, CREDENTIAL_JSON, CREDENTIAL_HASH, CREDENTIAL_REASON, ENTRY_DATE, PROCESS_DATE, PROCESS_SUCCESS)
                VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING RECORD_ID;"""
        # create row(s) for corp creds json info
        cred_json = json.dumps(credential, cls=CustomJsonEncoder, sort_keys=True)
        cred_hash = hashlib.sha256(cred_json.encode('utf-8')).hexdigest()
        try:
            cur.execute("savepoint save_" + cred_type)
            # store address creds with a special status, because we don't want to post them yet
            if cred_type == addr_credential:
                cur.execute(sql_addr, (system_cd, event_json(prev_event), event_json(last_event), corp_num, corp_state, cred_type, cred_id, 
                            schema_name, schema_version, cred_json, cred_hash, credential_reason, datetime.datetime.now(), datetime.datetime.now(), 'A',))
            else:
                cur.execute(sql, (system_cd, event_json(prev_event), event_json(last_event), corp_num, corp_state, cred_type, cred_id, 
                            schema_name, schema_version, cred_json, cred_hash, credential_reason, datetime.datetime.now(),))
            return 1
        except Exception as e:
            # ignore duplicate hash ("duplicate key value violates unique constraint "cl_hash_index"")
            # re-raise all others
            stre = str(e)
            if "duplicate key value violates unique constraint" in stre and "cl_hash_index" in stre:
                print("Hash exception, skipping duplicate credential for corp:", corp_num, cred_type, cred_id, e)
                cur.execute("rollback to savepoint save_" + cred_type)
                print(cred_json)
                return 0
            else:
                print(traceback.print_exc())
                raise

    # determine jurisdiction for corp
    def get_corp_jurisdiction(self, corp, jurisdiction):
        registered_jurisdiction = ""
        if corp['corp_type']['corp_class'] == 'BC':
            registered_jurisdiction = "BC"
        elif corp['corp_type']['corp_class'] == 'XPRO':
            if jurisdiction is not None and 'can_jur_typ_cd' in jurisdiction:
                if jurisdiction['can_jur_typ_cd'] == 'OT':
                    if 'othr_juris_desc' in jurisdiction and jurisdiction['othr_juris_desc'] is not None:
                        registered_jurisdiction = jurisdiction['othr_juris_desc']
                    else:
                        registered_jurisdiction = jurisdiction['can_jur_typ_cd']
                else:
                    registered_jurisdiction = jurisdiction['can_jur_typ_cd']
        else:
            # default to BC
            registered_jurisdiction = "BC"
        return registered_jurisdiction

    # corp num with prefix
    def corp_num_with_prefix(self, corp_typ_cd, corp_num):
        p_corp_num = corp_num
        if corp_typ_cd == 'BC':
            p_corp_num = 'BC' + corp_num
        elif corp_typ_cd == 'ULC':
            p_corp_num = 'BC' + corp_num
        elif corp_typ_cd == 'CC':
            p_corp_num = 'BC' + corp_num
        return p_corp_num

    # determine reason for address credential - returns reason code only
    def build_corp_reason_code(self, loop_start_event):
        corp_reason = ""

        # corp_state reason code
        if 'filing_typ_cd' in loop_start_event['filing']:
            corp_reason = 'Filing:' + loop_start_event['filing']['filing_typ_cd'] 
        else:
            corp_reason = 'Event:' + loop_start_event['event_typ_cd'] 

        if (len(corp_reason) > 0 and corp_reason.startswith(", ")):
            corp_reason = corp_reason[2:]
        if (255 < len(corp_reason)):
            endch = corp_reason[len(corp_reason)-1]
            corp_reason = corp_reason[:251] + '...' + endch

        return corp_reason
        
    def check_required_field(self, corp_num, corp_cred, cred_attr):
        if cred_attr not in corp_cred or corp_cred[cred_attr] is None or corp_cred[cred_attr] == '':
            print(">>>Data Issue:Credential:" + corp_num + ":" + cred_attr + ":", corp_cred)

    def compare_dates(self, first_date, op, second_date, msg):
        if first_date is None:
            print(msg, "first date is None")
        if second_date is None:
            print(msg, "second date is None")
        if op == "==" or op == '=':
            return first_date == second_date
        elif op == "<=":
            return first_date <= second_date
        elif op == "<":
            return first_date < second_date
        elif op == ">":
            return first_date > second_date
        elif op == ">=":
            return first_date >= second_date
        print(msg, "invalid date op", op)
        return False

    # generate address credential
    def generate_address_credential(self, corp_num, corp_info, office, address, dba_corp_num, dba_name):
        addr_cred = {}
        addr_cred['registration_id'] = self.corp_num_with_prefix(corp_info['corp_typ_cd'], corp_num)
        if 0 < len(corp_info['org_names']):
            org_name = self.corp_rec_at_effective_date(corp_info['org_names'], office['start_event'])
            if org_name is not None:
                addr_cred['addressee'] = org_name['corp_nme']
        addr_cred['address_type'] = office['office_type']['office_typ_cd']
        addr_cred['civic_address'] = address['local_addr']
        if 'city' in address:
            addr_cred['municipality'] = address['city']
        if 'province' in address:
            addr_cred['province'] = address['province']
        if 'postal_cd' in address:
            addr_cred['postal_code'] = address['postal_cd']
        if 'country_typ_cd' in address:
            addr_cred['country'] = address['country_typ_cd']
        addr_cred['address_effective'] = self.filter_min_date(office['effective_start_date'])
        addr_cred['effective_date'] = addr_cred['address_effective']
        if office['end_event_id'] is not None and office['end_event']['effective_date'] <= corp_info['current_date']:
            addr_cred['expiry_date'] = office['effective_end_date']

        return addr_cred

    # store credentials for the provided corp
    def store_credentials(self, cur, system_typ_cd, prev_event, last_event, corp_num, corp_state, corp_info, corp_creds):
        cred_count = 0
        for corp_cred in corp_creds:
            if corp_cred['credential']['effective_date'] is not None and corp_cred['credential']['effective_date'] != '':
                cred_count = cred_count + self.insert_json_credential(cur, system_typ_cd, prev_event, last_event, corp_num, corp_state, 
                                                                corp_cred['cred_type'], corp_cred['id'], corp_cred['schema'], corp_cred['version'], 
                                                                corp_cred['credential'], corp_cred['credential_reason'])
            else:
                print("Error can't issue a credential with no effective date!")
        return cred_count

    def build_credential_dict(self, cred_type, schema, version, cred_id, credential, credential_reason, effective_date):
        cred = {}
        cred['cred_type'] = cred_type
        cred['schema'] = schema
        cred['version'] = version
        cred['credential'] = credential
        cred['credential_reason'] = credential_reason
        cred['id'] = cred_id
        cred['effective_date'] = effective_date
        return cred

    # credential effective date is the latest of the individual effective dates in the credential
    def credential_effective_date(self, corp_cred):
        effective_date = None
        if corp_cred['entity_status_effective'] is not None and corp_cred['entity_status_effective'] != '':
            effective_date = corp_cred['entity_status_effective']
        if effective_date is None or ('entity_name_effective' in corp_cred and corp_cred['entity_name_effective'] != '' and self.compare_dates(effective_date, "<", corp_cred['entity_name_effective'], 'cred:' + str(corp_cred))):
            effective_date = corp_cred['entity_name_effective']
        if effective_date is None or ('entity_name_assumed_effective' in corp_cred and self.compare_dates(effective_date, "<", corp_cred['entity_name_assumed_effective'], 'cred:' + str(corp_cred))):
            effective_date = corp_cred['entity_name_assumed_effective']
        return effective_date

    def unique_effective_recs(self, rec_type, rec_attr, corp_records, effective_recs):
        for corp_record in corp_records:
            rec_summary = {}
            rec_summary['corp_num'] = corp_record['corp_num']
            rec_summary['rec_type'] = rec_type
            rec_summary['effective_start_date'] = corp_record['effective_start_date']
            rec_summary['effective_end_date']   = corp_record['effective_end_date']
            rec_summary['desc']     = corp_record[rec_attr]
            effective_recs.append(rec_summary)

        # sort to get in date order, and determine ACT/HIS transition dates
        #print('>>>>>>>>>> unique_effective_recs start', rec_type)
        effective_recs = sorted(effective_recs, key=lambda k: k['effective_end_date'])
        effective_recs = sorted(effective_recs, key=lambda k: k['effective_start_date'])
        #print('<<<<<<<<<< unique_effective_recs end', rec_type)

        return effective_recs

    # build a list of unique effective/expiry dates
    def unique_effective_events(self, corp_records, effective_events):
        for corp_record in corp_records:
            effective_events.append(corp_record['start_event'])
            if corp_record['end_event_id'] is not None:
                effective_events.append(corp_record['end_event'])

        # sort to get in date order, and determine ACT/HIS transition dates
        effective_events = sorted(effective_events, key=lambda k: k['event_timestmp'])
        effective_events = sorted(effective_events, key=lambda k: k['effective_date'])
        # eliminate duplicates
        ret_effective_events = []
        prev_effective_event = None
        for effective_event in effective_events:
            if prev_effective_event is None or effective_event['effective_date'] != prev_effective_event['effective_date']:
                ret_effective_events.append(effective_event)
            prev_effective_event = effective_event
        return ret_effective_events

    # org_names etc. active at effective date
    def corp_rec_at_effective_date(self, corp_recs, loop_start_event):
        # pick the lowest effective date where the passed in start date is in range
        loop_start_date = loop_start_event['effective_date']
        loop_start_id = loop_start_event['event_id']
        ret_corp_rec = None

        # find record matching the provided event
        for corp_rec in corp_recs:
            # ignore the record if start_date > end_date
            if self.compare_dates(corp_rec['effective_start_date'], "<=", corp_rec['effective_end_date'], str(corp_rec)):
                #if corp_rec['start_event_id'] == loop_start_id:
                #    # if the start event id matches then we have a match
                #    return corp_rec
                if self.compare_dates(corp_rec['effective_start_date'], "<=", loop_start_date, str(corp_rec)):
                    # if the record date is earlier than the event effective date, it is potential match
                    if 'end_event_id' not in corp_rec or corp_rec['end_event_id'] is None:
                        # if we hit the active record, use it (ignore anything dated after the start date of the currently active record)
                        ret_corp_rec = corp_rec
                        break
                    elif ret_corp_rec is None:
                        ret_corp_rec = corp_rec
                    elif self.compare_dates(corp_rec['effective_start_date'], ">", ret_corp_rec['effective_start_date'], str(corp_rec)):
                        # pick the latest record based on effective date
                        ret_corp_rec = corp_rec
                    elif self.compare_dates(corp_rec['effective_start_date'], "==", ret_corp_rec['effective_start_date'], str(corp_rec)):
                        if self.compare_dates(corp_rec['effective_end_date'], ">", ret_corp_rec['effective_end_date'], str(corp_rec)):
                            # if the start dates are the same, select the latest end date
                            ret_corp_rec = corp_rec

        if ret_corp_rec is None and len(corp_recs) > 0:
            if 'start_event' in corp_recs[0] and is_data_conversion_event(corp_recs[0]['start_event']):
                if corp_recs[0]['effective_start_date'] == corp_recs[0]['start_event']['effective_date']:
                    ret_corp_rec = corp_recs[0]

        return ret_corp_rec

    # currently active state record
    def get_corp_active_state(self, corp_info):
        ret_corp_state = None
        for corp_state in corp_info['corp_state']:
            if corp_state['end_event_id'] is None:
                return corp_state
            elif ret_corp_state is None:
                ret_corp_state = corp_state
            elif corp_state['effective_start_date'] > ret_corp_state['effective_start_date']:
                ret_corp_state = corp_state
        return ret_corp_state

    def corp_unique_record_list(self, corp_num, corp_info):
        # generate a list of (sorted) effective dates for our corp registration credentials
        effective_recs = self.unique_effective_recs('corp_state', 'state_typ_cd', corp_info['corp_state'], [])
        effective_recs = self.unique_effective_recs('jurisdiction', 'can_jur_typ_cd', corp_info['jurisdiction'], effective_recs)
        effective_recs = self.unique_effective_recs('org_names', 'corp_nme', corp_info['org_names'], effective_recs)
        effective_recs = self.unique_effective_recs('org_name_assumed', 'corp_nme', corp_info['org_name_assumed'], effective_recs)
        #effective_recs = self.unique_effective_recs('office', '', corp_info['office'], effective_recs)
        #effective_recs = self.unique_effective_recs('parties', '', corp_info['parties'], effective_recs)

        return effective_recs

    # determine the unique event list for the current corp
    def corp_unique_event_list(self, corp_num, corp_info):
        # generate a list of (sorted) effective dates for our corp registration credentials
        effective_events = self.unique_effective_events(corp_info['corp_state'], [])
        effective_events = self.unique_effective_events(corp_info['jurisdiction'], effective_events)
        effective_events = self.unique_effective_events(corp_info['org_names'], effective_events)
        effective_events = self.unique_effective_events(corp_info['org_name_assumed'], effective_events)
        #effective_events = self.unique_effective_events(corp_info['office'], effective_events)
        #effective_events = self.unique_effective_events(corp_info['parties'], effective_events)

        return effective_events

    def current_and_future_corp_events(self, corp_num, corp_info):
        # get events
        effective_events = self.corp_unique_event_list(corp_num, corp_info)

        # check if the effective date of any event is beyond "now()" (based on when the data was loaded from BC Reg)
        future_events = []
        past_events = []
        for event in effective_events:
            if event['effective_date'] > corp_info['current_date']:
                future_events.append(event)
            else:
                past_events.append(event)

        return (past_events, future_events)

    def filter_min_date(self, cred_date):
        if isinstance(cred_date, str):
            if cred_date == MIN_START_DATE_TZ:
                return ""
        #elif isinstance(cred_date, datetime.date):
        #    if cred_date == MIN_START_DATE:
        #        return ?TBD?
        return cred_date

    # generate credentials for the provided corp
    def generate_credentials(self, system_typ_cd, prev_event, last_event, corp_num, corp_info):
        corp_creds = []

        # get events - only generate credentials for events in the past
        (effective_events, future_events) = self.current_and_future_corp_events(corp_num, corp_info)

        if 0 < len(effective_events):
            #print('effective_events', effective_events)
            prev_effective_event = event_dict(effective_events[0]['event_id'], effective_events[0]['event_timestmp'])
            last_effective_event = event_dict(effective_events[len(effective_events)-1]['event_id'], effective_events[len(effective_events)-1]['event_timestmp'])
            if self.compare_dates(prev_effective_event['event_date'], ">", prev_event['event_date'], 'Events'):
                use_prev_event = prev_effective_event
            else:
                use_prev_event = prev_event
            if self.compare_dates(last_effective_event['event_date'], "<", last_event['event_date'], 'Events'):
                use_last_event = last_effective_event
            else:
                use_last_event = last_event

            # loop based on start/end events
            for i in range(len(effective_events)):
                #print('effective_event', effective_events[i])
                loop_start_event = effective_events[i]
                if (not (is_data_conversion_event(loop_start_event) and loop_start_event['event_timestmp'] == loop_start_event['effective_date'])) and use_prev_event['event_date'] <= loop_start_event['event_timestmp'] and loop_start_event['event_timestmp'] <= use_last_event['event_date']:
                    # generate corp credential
                    corp_cred = {}
                    corp_cred['registration_id'] = self.corp_num_with_prefix(corp_info['corp_typ_cd'], corp_info['corp_num'])
                    corp_cred['registration_date'] = self.filter_min_date(corp_info['recognition_dts'])

                    # org_names active at effective date
                    org_name = self.corp_rec_at_effective_date(corp_info['org_names'], loop_start_event)
                    if org_name is not None:
                        #print('org_name', org_name)
                        corp_cred['entity_name'] = org_name['corp_nme']
                        if is_data_conversion_event(org_name['start_event']) and org_name['effective_start_date'] == org_name['start_event']['effective_date']:
                            corp_cred['entity_name_effective'] = ''
                        else:
                            corp_cred['entity_name_effective'] = self.filter_min_date(org_name['effective_start_date'])
                    else:
                        corp_cred['entity_name'] = ''
                        corp_cred['entity_name_effective'] = ''

                    # org_name_assumed active at effective date
                    org_name_assumed = self.corp_rec_at_effective_date(corp_info['org_name_assumed'], loop_start_event)
                    if org_name_assumed is not None:
                        #print('org_name_assumed', org_name_assumed)
                        corp_cred['entity_name_assumed'] = org_name_assumed['corp_nme'] 
                        if is_data_conversion_event(org_name_assumed['start_event']) and org_name_assumed['effective_start_date'] == org_name_assumed['start_event']['entity_name_assumed_effective']:
                            corp_cred['entity_name_assumed_effective'] = ''
                        else:
                            corp_cred['entity_name_assumed_effective'] = self.filter_min_date(org_name_assumed['effective_start_date'])

                    # corp_state active at effective date
                    corp_state = self.corp_rec_at_effective_date(corp_info['corp_state'], loop_start_event)
                    if corp_state is not None:
                        #print('corp_state', corp_state)
                        corp_cred['entity_status'] = corp_state['op_state_typ_cd']
                        if is_data_conversion_event(corp_state['start_event']) and corp_state['effective_start_date'] == corp_state['start_event']['effective_date']:
                            corp_cred['entity_status_effective'] = ''
                        else:
                            corp_cred['entity_status_effective'] = self.filter_min_date(corp_state['effective_start_date'])
                    else:
                        corp_cred['entity_status'] = ''
                        corp_cred['entity_status_effective'] = ''

                    corp_cred['entity_type'] = corp_info['corp_type']['corp_typ_cd']

                    # jurisdiction active at effective date
                    jurisdiction = self.corp_rec_at_effective_date(corp_info['jurisdiction'], loop_start_event)
                    corp_cred['home_jurisdiction'] = self.get_corp_jurisdiction(corp_info, jurisdiction)
                    if corp_cred['home_jurisdiction'] != 'BC':
                        corp_cred['registered_jurisdiction'] = 'BC' 
                    else:
                        corp_cred['registered_jurisdiction'] = '' 
                    corp_cred['extra_jurisdictional_registration'] = ''

                    # make sure we set an effective date for the credential!
                    corp_cred['effective_date'] = self.credential_effective_date(corp_cred)
                    if corp_cred['effective_date'] is None or corp_cred['effective_date'] == '':
                        corp_cred['effective_date'] = loop_start_event['effective_date']
                    if corp_cred['effective_date'] is None or corp_cred['effective_date'] == '':
                        corp_cred['effective_date'] = corp_cred['registration_date']

                    reason_description = self.build_corp_reason_code(loop_start_event)

                    #self.check_required_field(corp_num, corp_cred, 'registration_date')
                    #self.check_required_field(corp_num, corp_cred, 'entity_name')
                    #self.check_required_field(corp_num, corp_cred, 'entity_status')

                    corp_cred = self.build_credential_dict(corp_credential, corp_schema, corp_version, corp_num, corp_cred, reason_description, corp_cred['effective_date'])

                    # these will be sorted by date, but we need to make sure we are not submitting duplicates
                    # checking against the previously generated credential is sufficient
                    #print('credential', corp_cred['credential'])
                    if (len(corp_creds) == 0) or (len(corp_creds) > 0 and corp_cred['credential'] != corp_creds[len(corp_creds)-1]['credential']):
                        corp_creds.append(corp_cred)

        # generate addr credential(s)
        corp_offices = sorted(corp_info['office'], key=lambda k: int(k['start_event_id']))
        corp_offices = sorted(corp_info['office'], key=lambda k: k['effective_start_date'])
        for office in corp_offices:
            if ((prev_event['event_date'] <= office['start_event']['event_timestmp'] and office['start_event']['event_timestmp'] <= last_event['event_date']) or
                (office['end_event_id'] is not None and prev_event['event_date'] <= office['end_event']['event_timestmp'] and office['end_event']['event_timestmp'] <= last_event['event_date'])):
                # ensure address history is generated correctly
                if 'office_typ_cd' in office:
                    if 'delivery_addr' in office and 'local_addr' in office['delivery_addr']:
                        addr_cred = self.generate_address_credential(corp_num, corp_info, office, office['delivery_addr'], "", "")
                        reason_description = self.build_corp_reason_code(office['start_event'])
                        corp_creds.append(self.build_credential_dict(addr_credential, addr_schema, addr_version, 
                                                                    corp_num + ',' + office['office_typ_cd'], 
                                                                    addr_cred, reason_description, addr_cred['effective_date']))
        
        corp_type = corp_info['corp_typ_cd']
        if corp_type == 'SP' or corp_type == 'MF':
            is_parent = False
        else:
            is_parent = True

        # generate relationship credential(s) (only for parent right now):
        if is_parent:
            if 'parties' in corp_info:
                # ensure relationship history is generated correctly
                corp_parties = sorted(corp_info['parties'], key=lambda k: int(k['start_event_id']))
                corp_parties = sorted(corp_info['parties'], key=lambda k: k['effective_start_date'])
                # first check if party records are for unique firms
                party_count = {}
                for party in corp_parties:
                    if ((prev_event['event_date'] <= party['start_event']['event_timestmp'] and party['start_event']['event_timestmp'] <= last_event['event_date']) or
                        (party['end_event_id'] is not None and prev_event['event_date'] <= party['end_event']['event_timestmp'] and party['end_event']['event_timestmp'] <= last_event['event_date'])):
                        if party['corp_info']['corp_typ_cd'] == 'SP' or party['corp_info']['corp_typ_cd'] == 'MF':
                            if party['corp_info']['corp_num'] in party_count:
                                party_count[party['corp_info']['corp_num']] = party_count[party['corp_info']['corp_num']] + 1
                            else:
                                party_count[party['corp_info']['corp_num']] = 1

                for party in corp_parties:
                    if ((prev_event['event_date'] <= party['start_event']['event_timestmp'] and party['start_event']['event_timestmp'] <= last_event['event_date']) or
                        (party['end_event_id'] is not None and prev_event['event_date'] <= party['end_event']['event_timestmp'] and party['end_event']['event_timestmp'] <= last_event['event_date'])):
                        if party['corp_info']['corp_typ_cd'] == 'SP' or party['corp_info']['corp_typ_cd'] == 'MF':
                            dba_cred = {}
                            dba_cred['registration_id'] = self.corp_num_with_prefix(corp_info['corp_typ_cd'], corp_info['corp_num'])
                            dba_cred['associated_registration_id'] = self.corp_num_with_prefix(party['corp_info']['corp_typ_cd'], party['corp_info']['corp_num'])
                            dba_cred['relationship'] = 'Owns'
                            dba_cred['relationship_description'] = 'Does Business As'
                            dba_cred['relationship_status'] = 'ACT'
                            dba_cred['effective_date'] = party['effective_start_date']

                            # if the start event is 'ADMIN' type and there is only one party record, use the firm effective date
                            if party['start_event']['event_typ_cd'] == 'ADMIN' and party_count[party['corp_info']['corp_num']] == 1:
                                dba_cred['effective_date'] = party['corp_info']['recognition_dts']

                            dba_cred['relationship_status_effective'] = self.filter_min_date(dba_cred['effective_date'])
                            if party['end_event_id'] is not None and party['end_event']['effective_date'] <= corp_info['current_date']:
                                dba_cred['expiry_date'] = party['effective_end_date']
                            reason_description = self.build_corp_reason_code(party['start_event'])
                            corp_creds.append(self.build_credential_dict(dba_credential, dba_schema, dba_version, dba_cred['registration_id'], dba_cred, reason_description, dba_cred['effective_date']))

        return corp_creds

    # process corps that have been queued - update data from bc_registries
    def process_corp_event_queue_internal(self, load_regs=True, generate_creds=False, use_cache=False):
        sql1 = """SELECT RECORD_ID, 
                         SYSTEM_TYPE_CD, 
                         PREV_EVENT_ID, 
                         PREV_EVENT_DATE, 
                         LAST_EVENT_ID, 
                         LAST_EVENT_DATE, 
                         CORP_NUM, 
                         ENTRY_DATE
                  FROM EVENT_BY_CORP_FILING
                  WHERE RECORD_ID IN
                  (
                    SELECT RECORD_ID
                    FROM EVENT_BY_CORP_FILING 
                    WHERE PROCESS_DATE is null
                    ORDER BY RECORD_ID
                    LIMIT !BS!
                  )
                  ORDER BY RECORD_ID;"""

        sql1a = """SELECT RECORD_ID, 
                          SYSTEM_TYPE_CD, 
                          PREV_EVENT, 
                          LAST_EVENT, 
                          CORP_NUM, 
                          CORP_JSON, 
                          ENTRY_DATE
                   FROM CORP_HISTORY_LOG
                   WHERE RECORD_ID IN
                   (
                     SELECT RECORD_ID
                     FROM CORP_HISTORY_LOG 
                     WHERE PROCESS_DATE is null
                     ORDER BY RECORD_ID
                     LIMIT !BS!
                   )
                   ORDER BY RECORD_ID;"""

        sql2 = """INSERT INTO CORP_HISTORY_LOG (SYSTEM_TYPE_CD, PREV_EVENT, LAST_EVENT, CORP_NUM, CORP_STATE, CORP_JSON, ENTRY_DATE)
                  VALUES(%s, %s, %s, %s, %s, %s, %s) RETURNING RECORD_ID;"""
        sql2a = """INSERT INTO CORP_HISTORY_LOG (SYSTEM_TYPE_CD, PREV_EVENT, LAST_EVENT, CORP_NUM, CORP_STATE, CORP_JSON, ENTRY_DATE, PROCESS_DATE, PROCESS_SUCCESS, PROCESS_MSG)
                  VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING RECORD_ID;"""
        sql2b = """INSERT INTO EVENT_BY_CORP_FILING (SYSTEM_TYPE_CD, PREV_EVENT_ID, PREV_EVENT_DATE, LAST_EVENT_ID, LAST_EVENT_DATE, CORP_NUM, ENTRY_DATE)
                 VALUES(%s, %s, %s, %s, %s, %s, %s) RETURNING RECORD_ID;"""

        sql3 = """UPDATE EVENT_BY_CORP_FILING
                  SET PROCESS_DATE = %s, PROCESS_SUCCESS = %s, PROCESS_MSG = %s
                  WHERE RECORD_ID = %s"""
        sql3a = """UPDATE CORP_HISTORY_LOG
                  SET PROCESS_DATE = %s, PROCESS_SUCCESS = %s, PROCESS_MSG = %s
                  WHERE RECORD_ID = %s"""

        cur = None
        start_time = time.perf_counter()
        processing_time = 0
        max_processing_time = 10 * 60
        continue_loop = True
        max_batch_size = CORP_BATCH_SIZE
        use_cache_param = use_cache
        while continue_loop and processing_time < max_processing_time:
            corps = []
            specific_corps = []

            # load data from BC Registries for the corporations we need to process (max of 3000 per chunk)
            # this data may be pulled directly, or pulled from a "cache" in the event processor database
            if load_regs:
                try:
                    # we are loading data from BC Registries based on the corp event queue
                    cur = self.conn.cursor()
                    cur.execute(sql1.replace("!BS!", str(max_batch_size)))
                    row = cur.fetchone()
                    while row is not None:
                        # include the date(s) for the start and end events
                        corps.append({'RECORD_ID':row[0], 'SYSTEM_TYPE_CD':row[1], 'PREV_EVENT': event_dict(row[2], row[3]), 'LAST_EVENT': event_dict(row[4], row[5]), 
                                        'CORP_NUM':row[6], 'ENTRY_DATE':row[7]})
                        specific_corps.append(row[6])
                        row = cur.fetchone()
                    cur.close()
                    cur = None
                except (Exception, psycopg2.DatabaseError) as error:
                    print(error)
                    print(traceback.print_exc())
                    raise
                finally:
                    if cur is not None:
                        cur.close()
            else:
                try:
                    # not loading from BC Reg, just processing data already loaded in corp_history
                    cur = self.conn.cursor()
                    cur.execute(sql1a.replace("!BS!", str(max_batch_size)))
                    row = cur.fetchone()
                    while row is not None:
                        # includes the date(s) for the start and end events
                        corps.append({'RECORD_ID':row[0], 'SYSTEM_TYPE_CD':row[1], 'PREV_EVENT':row[2], 'LAST_EVENT':row[3], 
                                    'CORP_NUM':row[4], 'CORP_JSON':row[5], 'ENTRY_DATE':row[6]})
                        specific_corps.append(row[4])
                        row = cur.fetchone()
                    cur.close()
                    cur = None
                except (Exception, psycopg2.DatabaseError) as error:
                    print(error)
                    print(traceback.print_exc())
                    raise
                finally:
                    if cur is not None:
                        cur.close()

            if len(specific_corps) == 0:
                continue_loop = False
            else:
                saved_creds = 0
                force_continue = False
                # now generate credentials from the corporate data
                with BCRegistries(use_cache) as bc_registries:
                    if use_cache:
                        try:
                            bc_registries.cache_bcreg_corps(specific_corps)
                        except (Exception, psycopg2.DatabaseError, psycopg2.DataError) as error:
                            # raises a SQL error if error during caching
                            print(error)
                            print(traceback.print_exc())
                            force_continue = True
                            if max_batch_size == CORP_BATCH_SIZE:
                                print("Error during caching operation, switching to smaller cache size")
                                corps = []
                                max_batch_size = FALLBACK_CORP_BATCH_SIZE
                            else:
                                print("Error during caching operation, switching to non-cached mode")
                                corps = []
                                use_cache = False

                    for i,corp in enumerate(corps): 
                        process_success = True
                        process_msg = None
                        if (i % 100 == 0) or (i+1 == len(corps)):
                            processing_time = time.perf_counter() - start_time
                            print('Processing: ' + str(processing_time))
                            print('>>> Processing {} of {} corporations.'.format(i+1, len(corps)))

                        if load_regs:
                            try:
                                # fetch corp info from bc_registries
                                corp_info = bc_registries.get_bc_reg_corp_info(corp['CORP_NUM'])

                                # get event summary
                                effective_recs = self.corp_unique_record_list(corp['CORP_NUM'], corp_info)
                                corp_info['reg_summary'] = effective_recs

                                corp_info_json = bc_registries.to_json(corp_info)
                                prev_event_json = event_json(corp['PREV_EVENT'])
                                last_event_json = event_json(corp['LAST_EVENT'])
                            except (Exception, psycopg2.DatabaseError) as error:
                                print(error)
                                print(traceback.print_exc())
                                process_success = False
                                process_msg = str(error)
                                #raise
                        else:
                            # json blob is cached in event processor database
                            corp_info = corp['CORP_JSON']
                            corp_info_json = bc_registries.to_json(corp_info)
                            prev_event_json = corp['PREV_EVENT']
                            last_event_json = corp['LAST_EVENT']

                        if process_success:
                            # get events - only generate credentials for events in the past
                            (effective_events, future_events) = self.current_and_future_corp_events(corp['CORP_NUM'], corp_info)

                            corp_active_state = self.get_corp_active_state(corp_info)

                            # if corporation is "withdrawn" then don't create any events
                            withdrawn_corp = corp_active_state['state_typ_cd'] == 'HWT'
                            if withdrawn_corp:
                                # setting these to empty arrays will force a status update with no creds generated
                                effective_events = []
                                future_events = []

                            if generate_creds:
                                corp_creds = []
                                if 0 < len(effective_events):
                                    try:
                                        # generate and store credentials
                                        corp_creds = self.generate_credentials(corp['SYSTEM_TYPE_CD'], corp['PREV_EVENT'], corp['LAST_EVENT'], corp['CORP_NUM'], corp_info)
                                        if len(corp_creds) > 0:
                                            cur = self.conn.cursor()
                                            saved_creds = saved_creds + self.store_credentials(cur, corp['SYSTEM_TYPE_CD'], corp['PREV_EVENT'], corp['LAST_EVENT'], 
                                                                    corp['CORP_NUM'], corp_active_state['op_state_typ_cd'], corp_info, corp_creds)
                                            cur.close()
                                            cur = None
                                    except (Exception, psycopg2.DatabaseError) as error:
                                        print(error)
                                        print(traceback.print_exc())
                                        process_success = False
                                        process_msg = str(error)
                                        #raise
                                    finally:
                                        if cur is not None:
                                            cur.close()

                                # store corporate info 
                                if process_success:
                                    flag = 'Y'
                                    if withdrawn_corp:
                                        res = 'Withdrawn'
                                    else:
                                        res = None
                                else:
                                    flag = 'N'
                                    if 255 < len(process_msg):
                                        res = process_msg[:250] + '...'
                                    else:
                                        res = process_msg
                                if load_regs:
                                    cur = self.conn.cursor()
                                    if 0 < len(corp_creds) or 0 == len(future_events):
                                        cur.execute(sql2a, (corp['SYSTEM_TYPE_CD'], prev_event_json, last_event_json, corp['CORP_NUM'], 
                                                            corp_active_state['op_state_typ_cd'], corp_info_json, datetime.datetime.now(), datetime.datetime.now(), flag, res,))
                                    cur.close()
                                    cur = None
                                else:
                                    # update process date
                                    cur = self.conn.cursor()
                                    if 0 < len(corp_creds) or 0 == len(future_events):
                                        cur.execute(sql3a, (datetime.datetime.now(), flag, res, corp['RECORD_ID'], ))
                                    cur.close()
                                    cur = None
                                if (0 < len(future_events)) and (0 < len(corp_creds) or load_regs):
                                    # create another record to handle future events (will do a re-load)
                                    future_events = sorted(future_events, key=lambda k: int(k['event_id']))
                                    future_events = sorted(future_events, key=lambda k: k['event_timestmp'])
                                    cur = self.conn.cursor()
                                    cur.execute(sql2b, (corp['SYSTEM_TYPE_CD'], future_events[0]['event_id'], future_events[0]['event_timestmp'], 
                                                        future_events[len(future_events)-1]['event_id'], future_events[len(future_events)-1]['event_timestmp'],  
                                                        corp['CORP_NUM'], datetime.datetime.now(),))
                                    cur.close()
                                    cur = None
                            elif load_regs:
                                try:
                                    # store corporate info for future generation of credentials
                                    cur = self.conn.cursor()
                                    cur.execute(sql2, (corp['SYSTEM_TYPE_CD'], prev_event_json, last_event_json, corp['CORP_NUM'], 
                                                        corp_active_state['op_state_typ_cd'], corp_info_json, datetime.datetime.now(),))
                                    cur.close()
                                    cur = None
                                except (Exception, psycopg2.DatabaseError) as error:
                                    print(error)
                                    print(traceback.print_exc())
                                    process_success = False
                                    process_msg = str(error)
                                    raise
                                finally:
                                    if cur is not None:
                                        cur.close()

                        # update process date
                        cur = self.conn.cursor()
                        if process_success:
                            cur.execute(sql3, (datetime.datetime.now(), 'Y', None, corp['RECORD_ID'], ))
                        else:
                            if 255 < len(process_msg):
                                res = process_msg[:250] + '...'
                            else:
                                res = process_msg
                            cur.execute(sql3, (datetime.datetime.now(), 'N', res, corp['RECORD_ID'], ))
                        self.conn.commit()
                        cur.close()
                        cur = None

                processing_time = time.perf_counter() - start_time
                print('Processing: ' + str(processing_time))

                # if we are generating creds but didn't on the last loop, bail
                if generate_creds and 0 == saved_creds and not force_continue:
                    print("Didn't complete any activity this loop, so bail")
                    continue_loop = False

                # if we processed a set of corps in non-cached mode, try to switch back
                if len(corps) > 0 and not use_cache:
                    print("Restoring cache mode")
                    use_cache = use_cache_param


    # process corps that have been queued - update data from bc_registries
    def process_corp_event_queue(self, use_cache=False):
        self.process_corp_event_queue_internal(True, False, use_cache)

    # generate creds based on pre-processed data (no connect to bc reg)
    def process_corp_generate_creds(self):
        self.process_corp_event_queue_internal(False, True)

    # process corps that have been queued - update data from bc_registries - and generate credentials
    def process_corp_event_queue_and_generate_creds(self, use_cache=False):
        self.process_corp_event_queue_internal(True, True, use_cache)

    # insert a transform into the transform table
    def insert_credential_transform(self, system_type, credential_typ_cd, mapping_transform, schema_name, schema_version):
        """ insert a new event into the event table """
        sql = """INSERT INTO CREDENTIAL_TRANSFORM (SYSTEM_TYPE_CD, CREDENTIAL_TYPE_CD, MAPPING_TRANSFORM, SCHEMA_NAME, SCHEMA_VERSION)
                 VALUES(%s, %s, %s, %s, %s) RETURNING RECORD_ID;"""
        cur = None
        try:
            cur = self.conn.cursor()
            cur.execute(sql, (system_type, credential_typ_cd, mapping_transform, schema_name, schema_version,))
            _record_id = cur.fetchone()[0]
            self.conn.commit()
            cur.close()
            cur = None
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            print(traceback.print_exc())
            raise
        finally:
            if cur is not None:
                cur.close()
            cur = None

    def display_event_processing_status(self):
        tables = ['event_by_corp_filing', 'corp_history_log', 'credential_log']

        for table in tables:
            process_ct     = self.get_record_count(table, False)
            outstanding_ct = self.get_record_count(table, True)
            print('Table:', table, 'Processed:', process_ct, 'Outstanding:', outstanding_ct)

            sql = "select count(*) from " + table + " where process_success = 'N'"
            error_ct = self.get_sql_record_count(sql)
            print('      ', table, 'Process Errors:', error_ct)
            if 0 < error_ct:
                self.print_processing_errors(table)

    def get_outstanding_corps_record_count(self):
        return self.get_record_count('event_by_corp_filing')
        
    def get_outstanding_creds_record_count(self):
        return self.get_record_count('credential_log')
        
    def get_record_count(self, table, unprocessed=True):
        sql_ct_select = 'select count(*) from'
        sql_corp_ct_processed   = 'where process_date is not null'
        sql_corp_ct_outstanding = 'where process_date is null'

        if table == 'credential_log':
            sql_corp_ct_processed = sql_corp_ct_processed + " and process_success != 'A'"

        sql = sql_ct_select + ' ' + table + ' ' + (sql_corp_ct_outstanding if unprocessed else sql_corp_ct_processed)

        return self.get_sql_record_count(sql)

    def get_sql_record_count(self, sql):
        cur = None
        try:
            cur = self.conn.cursor()
            cur.execute(sql)
            ct = cur.fetchone()[0]
            cur.close()
            cur = None
            return ct
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            print(traceback.print_exc())
            raise
        finally:
            if cur is not None:
                cur.close()
            cur = None

    def print_processing_errors(self, table):
        sql = """select * from """ + table + """
                 where process_success = 'N'
                 order by process_date DESC
                 limit 20"""
        rows = self.get_sql_rows(sql)
        print("       Recent errors:")
        print(rows)

    def get_sql_rows(self, sql):
        cursor = None
        try:
            cursor = self.conn.cursor()
            cursor.execute(sql)
            desc = cursor.description
            column_names = [col[0] for col in desc]
            rows = [dict(zip(column_names, row))  
                for row in cursor]
            cursor.close()
            cursor = None
            return rows
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            print(traceback.print_exc())
            raise
        finally:
            if cursor is not None:
                cursor.close()
            cursor = None


