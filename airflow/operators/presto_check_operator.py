import logging

from airflow.hooks import PrestoHook
from airflow.models import BaseOperator
from airflow.utils import apply_defaults


class PrestoCheckOperator(BaseOperator):
    """
    Performs a simple check using sql code in a specific Presto database.

    :param sql: the sql to be executed
    :type sql: string
    :param presto_conn_id: reference to the Presto database
    :type presto_conn_id: string
    """

    template_fields = ('sql',)
    template_ext = ('.hql', '.sql',)
    ui_color = '#fff7e6'

    @apply_defaults
    def __init__(
            self, sql,
            presto_conn_id='presto_default',
            *args, **kwargs):
        super(PrestoCheckOperator, self).__init__(*args, **kwargs)

        self.presto_conn_id = presto_conn_id
        self.sql = sql

    def execute(self, context=None):
        hook = PrestoHook(presto_conn_id=self.presto_conn_id)
        logging.info('Executing SQL check: ' + self.sql)
        records = hook.get_first(hql=self.sql)
        logging.info("Record: " + str(records))
        if not records:
            raise Exception("The query returned None")
        elif not all([bool(r) for r in records]):
            exceptstr = "Test failed.\nQuery:\n{q}\nResults:\n{r!s}"
            raise Exception(exceptstr.format(q=self.sql, r=records))
        logging.info("Success.")


def _convert_to_float_if_possible(s):
    '''
    A small helper function to convert a string to a numeric value
    if appropriate

    :param s: the string to be converted
    :type s: str
    '''
    try:
        ret = float(s)
    except (ValueError, TypeError):
        ret = s
    return ret


class PrestoValueCheckOperator(BaseOperator):
    """
    Performs a simple value check using sql code.

    :param sql: the sql to be executed
    :type sql: string
    :param presto_conn_id: reference to the Presto database
    :type presto_conn_id: string
    """

    __mapper_args__ = {
        'polymorphic_identity': 'PrestoValueCheckOperator'
    }
    template_fields = ('sql',)
    template_ext = ('.hql', '.sql',)
    ui_color = '#fff7e6'

    @apply_defaults
    def __init__(
            self, sql, pass_value, tolerance=None,
            presto_conn_id='presto_default',
            *args, **kwargs):
        super(PrestoValueCheckOperator, self).__init__(*args, **kwargs)
        self.presto_conn_id = presto_conn_id
        self.sql = sql
        self.pass_value = _convert_to_float_if_possible(pass_value)
        tol = _convert_to_float_if_possible(tolerance)
        self.tol = tol if isinstance(tol, float) else None
        self.is_numeric_value_check = isinstance(self.pass_value, float)
        self.has_tolerance = self.tol is not None

    def execute(self, context=None):
        logging.info('Executing SQL check: ' + self.sql)
        hook = PrestoHook(presto_conn_id=self.presto_conn_id)
        records = hook.get_first(hql=self.sql)
        if not records:
            raise Exception("The query returned None")
        test_results = []
        except_temp = ("Test failed.\nPass value:{self.pass_value}\n"
                       "Query:\n{self.sql}\nResults:\n{records!s}")
        if not self.is_numeric_value_check:
            tests = [str(r) == self.pass_value for r in records]
        elif self.is_numeric_value_check:
            try:
                num_rec = [float(r) for r in records]
            except (ValueError, TypeError) as e:
                cvestr = "Converting a result to float failed.\n"
                raise Exception(cvestr+except_temp.format(**locals()))
            if self.has_tolerance:
                tests = [
                    r / (1 + self.tol) <= self.pass_value <= r / (1 - self.tol)
                    for r in num_rec]
            else:
                tests = [r == self.pass_value for r in num_rec]
        if not all(tests):
            raise Exception(except_temp.format(**locals()))


class PrestoIntervalCheckOperator(BaseOperator):
    """
    Checks that the values of metrics given as SQL expressions are within
    a certain tolerance of the ones from days_back before.

    :param table: the table name
    :type table: str
    :param days_back: number of days between ds and the ds we want to check
        against. Defaults to 7 days
    :type days_back: int
    :param metrics_threshold: a dictionary of ratios indexed by metrics
    :type metrics_threshold: dict
    :param presto_conn_id: reference to the Presto database
    :type presto_conn_id: string
    """

    __mapper_args__ = {
        'polymorphic_identity': 'PrestoIntervalCheckOperator'
    }
    template_fields = ('sql1', 'sql2')
    template_ext = ('.hql', '.sql',)
    ui_color = '#fff7e6'

    @apply_defaults
    def __init__(
            self, table, metrics_thresholds,
            date_filter_column='ds', days_back=-7,
            presto_conn_id='presto_default',
            *args, **kwargs):
        super(PrestoIntervalCheckOperator, self).__init__(*args, **kwargs)
        self.presto_conn_id = presto_conn_id
        self.table = table
        self.metrics_thresholds = metrics_thresholds
        self.metrics_sorted = sorted(metrics_thresholds.keys())
        self.date_filter_column = date_filter_column
        self.days_back = -abs(days_back)
        sqlexp = ', '.join(self.metrics_sorted)
        sqlt = ("SELECT {sqlexp} FROM {table}"
                " WHERE {date_filter_column}=").format(**locals())
        self.sql1 = sqlt + "'{{ ds }}'"
        self.sql2 = sqlt + "'{{ macros.ds_add(ds, "+str(self.days_back)+") }}'"

    def execute(self, context=None):
        hook = PrestoHook(presto_conn_id=self.presto_conn_id)
        logging.info('Executing SQL check: ' + self.sql2)
        row2 = hook.get_first(hql=self.sql2)
        logging.info('Executing SQL check: ' + self.sql1)
        row1 = hook.get_first(hql=self.sql1)
        if not row2:
            raise Exception("The query {q} returned None").format(q=self.sql2)
        if not row1:
            raise Exception("The query {q} returned None").format(q=self.sql1)
        current = dict(zip(self.metrics_sorted, row1))
        reference = dict(zip(self.metrics_sorted, row2))
        ratios = {}
        test_results = {}
        rlog = "Ratio for {0}: {1} \n Ratio threshold : {2}"
        fstr = "'{k}' check failed. {r} is above {tr}"
        estr = "The following tests have failed:\n {0}"
        countstr = "The following {j} tests out of {n} failed:"
        for m in self.metrics_sorted:
            if current[m] == 0 or reference[m] == 0:
                ratio = None
            else:
                ratio = float(max(current[m], reference[m])) / \
                    min(current[m], reference[m])
            logging.info(rlog.format(m, ratio, self.metrics_thresholds[m]))
            ratios[m] = ratio
            test_results[m] = ratio < self.metrics_thresholds[m]
        if not all(test_results.values()):
            failed_tests = [it[0] for it in test_results.items() if not it[1]]
            j = len(failed_tests)
            n = len(self.metrics_sorted)
            logging.warning(countstr.format(**locals()))
            for k in failed_tests:
                logging.warning(fstr.format(k=k, r=ratios[k],
                                tr=self.metrics_thresholds[k]))
            raise Exception(estr.format(", ".join(failed_tests)))
        logging.info("All tests have passed")
