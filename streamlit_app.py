# Only the relevant part of streamlit_app.py (the "All tickets" branch)

# Inside the "All tickets" branch:
else:
    st.info(f"📋 Fetching tickets for supplier {supplier_id}...")
    tickets = fetch_all_tickets(api, supplier_id, limit=limit)
    st.info(f"📋 Found {len(tickets)} ticket(s).")
    results = []
    progress_placeholder = st.empty()

    for idx, t in enumerate(tickets):
        code = t.get("code")
        if not code:
            st.warning(f"⚠️ Skipping ticket {idx+1}: no code field")
            results.append({"status": "skipped", "reason": "no code field", "raw": t})
            continue

        progress_placeholder.write(f"🔄 Processing ticket {idx+1}/{len(tickets)}: **{code}**")

        # Check if it's already up-to-date (quick check using state)
        # We'll call sync_ticket_from_data which does the check and returns quickly.
        main_result = sync_ticket_from_data(
            api, translator, store, supplier_id, t, target_languages,
            dry_run=dry_run, force=force
        )
        if main_result.get("status") == "up_to_date":
            st.write(f"   ✅ Skipped – already translated.")
            # Still need to sync options? Options might not be up-to-date even if main is.
            # We'll always check options.
        else:
            st.write(f"   → Syncing main ticket...")
        results.append(main_result)

        # Sync options – we need to fetch them separately (they aren't in the list)
        st.write(f"   → Syncing options for {code}...")
        option_results = sync_all_options_for_ticket(
            api, translator, store, supplier_id, code, target_languages,
            dry_run=dry_run, force=force
        )
        if isinstance(main_result, dict):
            main_result["options"] = option_results
        else:
            results.extend(option_results)

        st.write(f"   ✅ Finished ticket {code}")
        st.divider()

    progress_placeholder.empty()
