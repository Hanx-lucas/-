from contextlib import ExitStack
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from history_dates import newer_price_title, explicit_label_date
import wechat_visual_rpa as rpa

class HistoryRangeTests(unittest.TestCase):
    def test_newer_dates_skip_but_unknown_and_conflicting_titles_are_verified(self):
        today=date(2026,10,11); end=date(2026,10,2)
        for title in ["茅台酒10月9日行情价", "2026年10月11日酒价", "10/09酒价参考", "１０月９日行情"]:
            self.assertTrue(newer_price_title(title,end,today),title)
        for title in ["10月1日酒价", "10月2日酒价", "酒价参考", "10月32日酒价", "10月1日与10月9日行情"]:
            self.assertFalse(newer_price_title(title,end,today),title)
        self.assertEqual(explicit_label_date("10月2日 09:30",today),end)
        self.assertFalse(newer_price_title("12月31日酒价",date(2026,1,2),date(2026,1,3)))

    def test_selected_october_1_2_never_opens_october_9_card(self):
        window=rpa.WindowInfo(0,"公众号","test",rpa.Rect(0,0,100,100))
        titles=["茅台酒10月9日行情价", "茅台酒10月2日行情价", "茅台酒10月1日行情价"]
        feed={"time_labels":[{"text":"星期五","center_y_1000":100}],
              "articles":[{"title":title,"center_y_1000":200+i*100,"screen_point":(10,10),"list_read_count":10,"list_like_count":2} for i,title in enumerate(titles)]}
        end={"time_labels":[{"text":"9月30日","center_y_1000":100}],"articles":[]}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            for name in ["activate_window","click","close_article_after_attempt","scroll_window_down","log_event"]:
                stack.enter_context(patch.object(rpa,name))
            stack.enter_context(patch.object(rpa.time,"sleep"))
            for name,value in [("PRICE_START",date(2026,10,1)),("PRICE_END",date(2026,10,2)),("PRICE_ONLY",True)]:
                stack.enter_context(patch.object(rpa,name,value))
            stack.enter_context(patch.object(rpa,"search_and_open_profile",return_value=(window,"易茅时价")))
            stack.enter_context(patch.object(rpa,"analyze_profile_window",side_effect=[feed,feed,end]))
            collect=stack.enter_context(patch.object(rpa,"collect_open_article",side_effect=lambda *a,**k:
                {"status":"inserted","title":k["expected_title"],"url":"https://mp.weixin.qq.com/s/"+k["expected_title"],"content":"正文"}))
            summary=rpa.collect_profile_account(None,"易茅时价",Path(directory),20,None,None)
        self.assertEqual([c.kwargs["expected_title"] for c in collect.call_args_list],titles[1:])
        self.assertEqual(len(summary["collected"]),2)

    def test_first_profile_read_refreshes_and_returns_to_top(self):
        window=rpa.WindowInfo(0,"公众号","test",rpa.Rect(0,0,100,100))
        feed={"time_labels":[{"text":"今天","center_y_1000":100}],"articles":[{"title":"行情","center_y_1000":200,"center_x_1000":500}]}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            for name in ["activate_window","log_event","press_ctrl_home"]:
                stack.enter_context(patch.object(rpa,name))
            stack.enter_context(patch.object(rpa.time,"sleep"))
            keyboard=stack.enter_context(patch.object(rpa.user32,"keybd_event"))
            scroll=stack.enter_context(patch.object(rpa,"scroll_window_up"))
            stack.enter_context(patch.object(rpa,"capture_window",return_value=Image.new("RGB",(100,100))))
            stack.enter_context(patch.object(rpa.PROFILE_OCR,"inspect_profile_feed",return_value=feed))
            rpa.analyze_profile_window(window,Path(directory),move_to_latest=True)
            self.assertEqual(keyboard.call_args_list[0].args[0],0x74)
            scroll.assert_called_once_with(window.rect,wheel_notches=100)

if __name__ == "__main__": unittest.main()
