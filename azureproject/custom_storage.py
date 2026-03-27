from django.contrib.staticfiles.storage import ManifestStaticFilesStorage

class IgnoreErrorsStaticFilesStorage(ManifestStaticFilesStorage):
    def post_process(self, paths, dry_run=False, **options):
        try:
            return super().post_process(paths, dry_run, **options)
        except Exception as e:
            print(f'Error: {e}')
            print('Ignoring error and continuing...')
            return []