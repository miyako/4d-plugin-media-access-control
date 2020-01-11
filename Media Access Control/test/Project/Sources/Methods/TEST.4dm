//%attributes = {"preemptive":"capable"}
ARRAY TEXT:C222($addresses;0)
ARRAY TEXT:C222($types;0)
ARRAY TEXT:C222($names;0)
ARRAY TEXT:C222($displayNames;0)

$address:=Get hardware address ($addresses;$types;$names;$displayNames)


ALERT:C41($addresses{1})