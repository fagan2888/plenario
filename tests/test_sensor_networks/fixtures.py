import os

from plenario.sensor_network.sensor_models import *

from datetime import datetime
from random import randint, random
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, ProgrammingError


class Fixtures:

    def _run_with_connection(self, query):
        conn = self.engine.connect()
        try:
            print query
            conn.execute("commit")
            conn.execute(query)
        except ProgrammingError as err:
            print err
        finally:
            conn.close()

    def _create_foi_table(self, table_schema):
        """A postgres friendly version of the redshift method that shares
        the same name.

        :param table_schema: (dict) {"name": "<name>", "properties": [ ... ]}
        :returns: None"""

        print "create table {}".format(table_schema["name"])

        create_table = ("CREATE TABLE {} ("
                        "\"node_id\" VARCHAR NOT NULL,"
                        "datetime TIMESTAMP WITHOUT TIME ZONE NOT NULL,"
                        "\"meta_id\" DOUBLE PRECISION NOT NULL,"
                        "\"sensor\" VARCHAR NOT NULL,"
                        "").format(table_schema["name"])

        for i, prop in enumerate(table_schema["properties"]):
            create_table += '"{}" {} '.format(prop['name'], prop['type'])
            create_table += "," if i != len(table_schema["properties"]) - 1 else ""
        create_table += ')'
        self.rs_engine.execute(create_table)

    def __init__(self):
        self.user = os.environ["DB_USER"] = "postgres"
        self.password = os.environ["DB_PASSWORD"] = "password"
        self.host = os.environ["DB_HOST"] = "localhost"
        self.port = os.environ["DB_PORT"] = "5432"
        os.environ["DB_NAME"] = "sensor_meta_test"

        self.user = os.environ["RS_USER"] = "postgres"
        self.password = os.environ["RS_PASSWORD"] = "password"
        self.host = os.environ["RS_HOST"] = "localhost"
        self.port = os.environ["RS_PORT"] = "5432"
        os.environ["RS_NAME"] = "sensor_obs_test"

        self.base_db_url = "postgresql://{}:{}@{}:{}".format(self.user, self.password, self.host, self.port)
        self.engine = create_engine(self.base_db_url)
        self.pg_engine = None
        self.rs_engine = None

    def setup_databases(self):
        self._run_with_connection("create database sensor_meta_test")
        self._run_with_connection("create database sensor_obs_test")
        self.rs_engine = create_engine(self.base_db_url + "/sensor_obs_test")
        self.pg_engine = create_engine(self.base_db_url + "/sensor_meta_test")
        self.pg_engine.execute("create extension postgis")

    def generate_sensor_network_meta_tables(self):
        print "Creating sensor network tables for {} ..." .format(self.pg_engine)
        Base.metadata.create_all(bind=self.pg_engine)

    def drop_databases(self):
        self._run_with_connection("drop database sensor_meta_test")
        self._run_with_connection("drop database sensor_obs_test")

    def generate_mock_metadata(self):
        session.configure(bind=self.pg_engine)

        feature_01 = FeatureOfInterest(
            name="temperature",
            observed_properties=[{"type": "float", "name": "temperature"}]
        )

        feature_02 = FeatureOfInterest(
            name="vector",
            observed_properties=[{"type": "float", "name": "x"}, {"type": "float", "name": "y"}]
        )

        sensor_01 = Sensor(
            name="sensor_01",
            observed_properties={"howhot": "temperature.temperature"}
        )

        sensor_02 = Sensor(
            name="sensor_02",
            observed_properties={"vec_x": "vector.x", "vex_y": "vector.y"}
        )

        node = NodeMeta(
            id="test_node",
            sensor_network="test_network",
            sensors=[sensor_01, sensor_02],
            location="0101000020E6100000A4A7C821E2E755C07C48F8DEDFF04440",
        )

        network = NetworkMeta(
            name="test_network",
            nodes=[node],
        )

        network_02 = NetworkMeta(
            name="test_network_other",
        )

        for obj in [feature_01, feature_02, network, network_02, node]:
            try:
                print "INSERT {} with {}".format(obj, session.get_bind())
                session.add(obj)
                session.commit()
            except IntegrityError as err:
                print str(err) + "\n"
                session.rollback()

    def generate_mock_observations(self):
        self._create_foi_table({"name": "vector", "properties": [{"name": "x", "type": "float"}, {"name": "y", "type": "float"}]})
        self._create_foi_table({"name": "temperature", "properties": [{"name": "temperature", "type": "float"}]})
        print "Populating records ",

        date_string = "2016-10-{} {}:{}:{}"
        date_format = "%Y-%m-%d %H:%M:%S"
        day = 0

        for i in range(0, 300):
            if i % 100 == 0:
                day += 1
                print ".",

            record_date = datetime.strptime(
                date_string.format(day, randint(0, 23), randint(0, 59), randint(0, 59)),
                date_format
            )

            self.rs_engine.execute("""
                insert into vector (node_id, datetime, meta_id, sensor, x, y)
                values ('test_node', '{}', '{}', 'sensor_02', {}, {})
                """.format(record_date, randint(0, 100), random(), random())
            )

            self.rs_engine.execute("""
                insert into temperature (node_id, datetime, meta_id, sensor, temperature)
                values ('test_node', '{}', '{}', 'sensor_02', {})
                """.format(record_date, randint(0, 100), random(), random())
            )

        print "\n"
