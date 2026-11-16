int main(){
 ExpectedLogin=123;ExpectedServer="Mock";PositionIdentifiers="77";
 Row o;o.strings={{ORDER_SYMBOL,"XAUUSD.s"},{ORDER_COMMENT,"Ntest"}};o.ints={{ORDER_MAGIC,123}};o.doubles={{POSITION_PRICE_OPEN,100},{POSITION_SL,98.5},{POSITION_TP,102}};mock_orders[1000]=o;fill_order(1000,77,1,.02);
 assert(OnInit()==INIT_SUCCEEDED);Observe(77);string path=MailboxFolder+"\\position-77.json";
 assert(mock_files[path].find("OBSERVED")!=std::string::npos);assert(mock_files[path].find("\"ownership_verified\":false")!=std::string::npos);
 exit_position(77,DEAL_REASON_TP,.3,.01);Observe(77);assert(mock_files[path].find("\"balanced_closed_volume\":false")!=std::string::npos);
 exit_position(77,DEAL_REASON_SL,-1.5,.01);Observe(77);assert(mock_files[path].find("\"balanced_closed_volume\":true")!=std::string::npos);string before=mock_files[path];
 for(int i=0;i<2048;i++){MqlTradeTransaction t;MqlTradeRequest q;MqlTradeResult r;OnTradeTransaction(t,q,r);}Observe(77);assert(mock_files[path].find("-1.2400000000")!=std::string::npos);
 history_available=false;Observe(77);assert(mock_files[path].find("HISTORY_UNAVAILABLE")!=std::string::npos);assert(mock_files[path].find("\"balanced_closed_volume\":false")!=std::string::npos);
 connected=false;Observe(77);assert(mock_files[path].find("DISCONNECTED")!=std::string::npos);OnDeinit(0);return 0;
}
