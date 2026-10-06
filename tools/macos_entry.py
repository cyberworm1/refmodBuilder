import sys

if '--smoke-test' in sys.argv:
    from refmod_builder.smoke import run
    run()
else:
    from refmod_builder.ui import main
    main()
