int main(){
 ExpectedLogin=123;ExpectedServer="Mock";TradeSymbol="XAUUSD.s";CycleId="1";PositionIdentifiers="77";
 Row p;p.ints={{POSITION_IDENTIFIER,77},{POSITION_TICKET,77},{POSITION_TYPE,POSITION_TYPE_BUY}};p.strings[POSITION_SYMBOL]="XAUUSD.s";p.doubles={{POSITION_PRICE_OPEN,100},{POSITION_VOLUME,.01},{POSITION_SL,98.5},{POSITION_TP,102}};mock_positions[77]=p;
 assert(OnInit()==INIT_SUCCEEDED);Scan();assert(revisions[0]==0);
 mock_quote.time_msc+=100;mock_quote.bid=100.26;mock_quote.ask=100.28;Scan();assert(reached[0]==1 && revisions[0]==1);
 string path=MailboxFolder+"\\sl-1-77.json";assert(mock_files[path].find("99.7500000000")!=std::string::npos);
 Scan();assert(revisions[0]==1); // unchanged recommendation retains its ID
 mock_quote.time_msc+=100;mock_quote.bid=100.10;mock_quote.ask=100.12;Scan();assert(reached[0]==1);assert(mock_files[path].find("99.7500000000")!=std::string::npos);
 mock_quote.time_msc+=100;mock_quote.bid=100.41;mock_quote.ask=100.43;Scan();assert(reached[0]==2 && revisions[0]==2); // crosses straight to second stage
 OnDeinit(0);assert(OnInit()==INIT_SUCCEEDED);assert(reached[0]==2);
 mock_positions[77].doubles[POSITION_SL]=100.1;Scan();assert(mock_files[path].find("NO_ACTION")!=std::string::npos);
 connected=false;ulong rev=revisions[0];mock_quote.bid=101;Scan();assert(revisions[0]==rev);
 connected=true;mock_quote.time_msc+=100;mock_quote.ask=101.02;Scan();assert(reached[0]==2);
 OnDeinit(0);
 // Mirrored SELL, on ASK; a lower BID by itself cannot trigger protection.
 mock_files.clear();mock_positions[77].ints[POSITION_TYPE]=POSITION_TYPE_SELL;mock_positions[77].doubles[POSITION_SL]=101.5;mock_positions[77].doubles[POSITION_TP]=98;
 mock_quote.bid=99.7;mock_quote.ask=100;quote_time=0;quote_changed_since_start=false;assert(OnInit()==INIT_SUCCEEDED);Scan();mock_quote.time_msc+=100;mock_quote.ask=99.76;Scan();assert(reached[0]==0);
 mock_quote.time_msc+=100;mock_quote.ask=99.60;mock_quote.bid=99.58;Scan();assert(reached[0]==2 && mock_files[path].find("100.0000000000")!=std::string::npos);
 OnDeinit(0);
 // Coalesced tick: current price retraced, but terminal history proves a
 // post-entry +0.40 peak. Repeated window reads do not duplicate the signal.
 mock_files.clear();mock_ticks.clear();mock_positions[77].ints[POSITION_TYPE]=POSITION_TYPE_BUY;mock_positions[77].ints[POSITION_TIME_MSC]=180000;mock_positions[77].doubles[POSITION_SL]=98.5;mock_positions[77].doubles[POSITION_TP]=102;
 mock_quote.bid=100.1;mock_quote.ask=100.12;mock_quote.time_msc=181000;quote_time=0;quote_changed_since_start=false;assert(OnInit()==INIT_SUCCEEDED);Scan();
 MqlTick peak=mock_quote;peak.time_msc=180500;peak.bid=100.45;peak.ask=100.47;mock_ticks.push_back(peak);
 mock_quote.time_msc+=100;Scan();assert(reached[0]==2);ulong stable=revisions[0];Scan();assert(revisions[0]==stable);
 ticks_available=false;mock_quote.time_msc+=100;ulong cursor_before=tick_cursors[0];Scan();assert(tick_cursors[0]==cursor_before);assert(mock_files[path].find("INCOMPLETE_RETRY_WINDOW")!=std::string::npos);
 OnDeinit(0);
 CycleId="";active_cycle="1";mock_files[MailboxFolder+"\\native-watch.txt"]="123|Mock|XAUUSD.s|1\n77\nEND";
 mock_files[MailboxFolder+"\\position-cycle-77.txt"]="123|Mock|XAUUSD.s|77|2";
 mock_files[MailboxFolder+"\\sl-monitor-settings.txt"]="123|Mock|XAUUSD.s|.50|.80|-.20|.10";ReadMonitorSettings();assert(monitor_first==.50 && monitor_second==.80 && monitor_first_offset==-.20 && monitor_second_offset==.10);
 ticks_available=true;Recommend(0,mock_quote);assert(healthy && reached[0]==0); // old .45 peak no longer crosses .50
 mock_quote.bid=100.51;mock_quote.ask=100.53;mock_quote.time_msc+=100;Recommend(0,mock_quote);assert(reached[0]==1 && mock_files[MailboxFolder+"\\sl-2-77.json"].find("99.8000000000")!=string::npos);
 mock_positions[77].doubles[POSITION_SL]=100.2;mock_quote.bid=100.81;mock_quote.ask=100.83;mock_quote.time_msc+=100;Recommend(0,mock_quote);assert(reached[0]==2 && mock_files[MailboxFolder+"\\sl-2-77.json"].find("NO_ACTION")!=string::npos);
 assert(PositionCycle(77)=="2");assert(RevisionFile(77)=="sl-state-2-77.txt");
 // First real OnTick is sufficient after a timer-only baseline, even with identical quote fields.
 mock_files.clear();mock_ticks.clear();CycleId="1";quote_time=0;quote_changed_since_start=false;mock_positions[77].doubles[POSITION_SL]=98.5;mock_quote.bid=100.41;mock_quote.ask=100.43;
 assert(OnInit()==INIT_SUCCEEDED);Scan();assert(!quote_changed_since_start);OnTick();assert(reached[0]==2);OnDeinit(0);
 return 0;
}
