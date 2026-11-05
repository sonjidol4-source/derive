import unittest
from realtime_investor_probe import summarize


class RealtimeProbeSummaryTests(unittest.TestCase):
    def test_reports_investor_events_and_requires_0780_check(self):
        events=[{'at':'2026-10-16T10:00:00+09:00','code':'001','kind':'투자자별매매','values':{'202':'-123'}}]
        text=summarize(events,['001'],'2026-10-16T10:00:00+09:00','2026-10-16T10:03:00+09:00',180)
        self.assertIn('투자자 관련 실시간 1건',text);self.assertIn('0780',text)

    def test_empty_result_points_to_next_path(self):
        text=summarize([],['001'],'2026-10-16T10:00:00+09:00','2026-10-16T10:03:00+09:00',180)
        self.assertIn('들어오지 않았습니다',text);self.assertIn('t1602',text)


if __name__=='__main__':unittest.main()
