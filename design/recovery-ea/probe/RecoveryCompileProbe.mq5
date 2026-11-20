#property strict
#property version "0.10"
#property description "Compile integration of RECOVERY modules; never submits orders."

#include "../execution/RecoveryActionRequest.mqh"
#include "../execution/RecoveryAdoptionInventory.mqh"
#include "../execution/RecoveryIntentRestart.mqh"
#include "../verifier/RecoveryExactOrderReader.mqh"

void OnStart()
  {
   Print("RECOVERY compile probe: no account or trade action.");
  }
