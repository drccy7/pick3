"""Make the form field `image` a FILE field (Cherri only emits text fields)."""
import plistlib, sys
for name in sys.argv[1:]:
    p = f"{name}_unsigned.shortcut"
    d = plistlib.load(open(p, "rb"))
    n = 0
    for a in d["WFWorkflowActions"]:
        if a["WFWorkflowActionIdentifier"] != "is.workflow.actions.downloadurl":
            continue
        for item in a["WFWorkflowActionParameters"]["WFFormValues"]["Value"]["WFDictionaryFieldValueItems"]:
            if item["WFKey"]["Value"]["string"] == "image":
                att = item["WFValue"]["Value"]["attachmentsByRange"]["{0, 1}"]
                item["WFItemType"] = 5
                item["WFValue"] = {"Value": att, "WFSerializationType": "WFTokenAttachmentParameterState"}
                n += 1
        for item in a["WFWorkflowActionParameters"]["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]:
            item["WFItemType"] = 0
        a["WFWorkflowActionParameters"]["WFFormValues"]["Value"]["WFDictionaryFieldValueItems"][0].setdefault("WFItemType", 5)
    assert n == 1, n
    d["WFWorkflowNoInputBehavior"] = {}  # absent = continue (back tap runs with no input)
    d.pop("WFWorkflowNoInputBehavior")
    plistlib.dump(d, open(f"{name}_patched.plist", "wb"), fmt=plistlib.FMT_XML)
    print("patched", name)
