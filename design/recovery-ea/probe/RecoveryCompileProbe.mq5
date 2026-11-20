#property strict
#property version "0.10"
#property description "Compile integration of RECOVERY modules; never submits orders."

#include "../execution/RecoveryActionRequest.mqh"
#include "../execution/RecoveryDispatch.mqh"
#include "../execution/RecoveryOpeningDispatch.mqh"
#include "../execution/RecoveryAlternatingDispatch.mqh"
#include "../execution/RecoverySupportRotationDispatch.mqh"
#include "../execution/RecoveryExactOrderEffect.mqh"
#include "../execution/RecoveryPositionEffect.mqh"
#include "../execution/RecoveryWatchedSupport.mqh"
#include "../cycle/RecoveryCompletedReverse.mqh"
#include "../cycle/RecoveryRejectedSupportMove.mqh"
#include "../cycle/RecoveryAlternatingFill.mqh"
#include "../cycle/RecoverySupportGap.mqh"
#include "../verifier/RecoveryClosedMoney.mqh"
#include "../execution/RecoveryAdoptionInventory.mqh"
#include "../execution/RecoveryIntentRestart.mqh"
#include "../verifier/RecoveryExactOrderReader.mqh"

void OnStart()
  {
   Print("RECOVERY compile probe: no account or trade action.");
  }
