import logging
import os.path
import shutil
from cattle.type_manager import get_type, MARSHALLER
from cattle.storage import BaseStoragePool
from cattle.agent.handler import KindBasedMixin
from cattle.plugins.volmgr import volmgr
from cattle.plugins.docker.util import is_no_op
from cattle.lock import lock
from cattle.progress import Progress
from . import docker_client, get_compute
from docker.errors import APIError

log = logging.getLogger('docker')


class DockerPool(KindBasedMixin, BaseStoragePool):
    def __init__(self):
        KindBasedMixin.__init__(self, kind='docker')
        BaseStoragePool.__init__(self)

    @staticmethod
    def _get_image_by_id(id):
        templates = docker_client().images(all=True)
        templates = filter(lambda x: x['Id'] == id, templates)

        if len(templates) > 0:
            return templates[0]
        return None

    def pull_image(self, image, progress):
        if not self._is_image_active(image, None):
            self._do_image_activate(image, None, progress)

    def _is_image_active(self, image, storage_pool):
        if is_no_op(image):
            return True
        parsed_tag = DockerPool.parse_repo_tag(image.data.dockerImage.fullName)
        try:
            if len(docker_client().inspect_image(parsed_tag['uuid'])):
                return True
        except APIError:
            pass
        return False

    def _do_image_activate(self, image, storage_pool, progress):
        if is_no_op(image):
            return

        auth_config = None
        try:
            if 'registryCredential' in image:
                if image.registryCredential is not None:
                    auth_config = {
                        'username': image.registryCredential['publicValue'],
                        'email': image.registryCredential['data']['fields']
                        ['email'],
                        'password': image.registryCredential['secretValue'],
                        'serveraddress': image.registryCredential['registry']
                        ['data']['fields']['serverAddress']
                    }
                    if auth_config['serveraddress'] == "https://docker.io":
                        auth_config['serveraddress'] =\
                            "https://index.docker.io"
                    log.debug('Auth_Config: [%s]', auth_config)
            else:
                log.debug('No Registry credential found. Pulling non-authed')
        except (AttributeError, KeyError, TypeError) as e:
            raise AuthConfigurationError("Malformed Auth Config. \n\n"
                                         "error: [%s]\nregistryCredential:"
                                         " %s"
                                         % (e, image.registryCredential))
        client = docker_client()
        data = image.data.dockerImage
        marshaller = get_type(MARSHALLER)
        temp = data.qualifiedName
        if data.qualifiedName.startswith('docker.io/'):
            temp = 'index.' + data.qualifiedName
        if progress is None:
            result = client.pull(repository=temp,
                                 tag=data.tag, auth_config=auth_config)
            if 'error' in result:
                raise ImageValidationError('Image [%s] failed to pull' %
                                           data.fullName)
        else:
            for status in client.pull(repository=temp,
                                      tag=data.tag,
                                      auth_config=auth_config,
                                      stream=True):
                log.info('Pulling [%s] status : %s', data.fullName, status)
                status = marshaller.from_string(status)
                try:
                    message = status['status']
                except KeyError:
                    message = status['error']
                    raise ImageValidationError('Image [%s] failed to pull '
                                               ': %s' % (data.fullName,
                                                         message))
                progress.update(message)

    def _get_image_storage_pool_map_data(self, obj):
        return {}

    def _get_volume_storage_pool_map_data(self, obj):
        return {
            'volume': {
                'format': 'docker'
            }
        }

    def _is_volume_active(self, volume, storage_pool):
        return True

    def _is_volume_inactive(self, volume, storage_pool):
        return True

    def _is_volume_removed(self, volume, storage_pool):
        if volume.deviceNumber == 0:
            container = get_compute().get_container(docker_client(),
                                                    volume.instance)
            return container is None
        else:
            path = self._path_to_volume(volume)
            # Check for volmgr managed volume, must be done before "isHostPath"
            if volmgr.volume_exists(path):
                return False
            if volume.data.fields['isHostPath']:
                # If this is a host path volume, we'll never really remove it
                # from disk, so just report is as removed for the purpose of
                # handling the event.
                return True

            return not os.path.exists(path)

    def _do_volume_remove(self, volume, storage_pool, progress):
        if volume.deviceNumber == 0:
            container = get_compute().get_container(docker_client(),
                                                    volume.instance)
            if container is None:
                return
            docker_client().remove_container(container)
        else:
            path = self._path_to_volume(volume)
            # Check for volmgr managed volume, must be done before "isHostPath"
            if volmgr.volume_exists(path):
                log.info("Deleting volmgr managed volume: %s" % path)
                volmgr.remove_volume(path)
                return
            if not volume.data.fields['isHostPath']:
                if os.path.exists(path):
                    log.info("Deleting volume: %s" % volume.uri)
                    shutil.rmtree(path)

    def _path_to_volume(self, volume):
        return volume.uri.replace('file://', '')

    @staticmethod
    def parse_repo_tag(image_uuid):
        if image_uuid.startswith('docker:'):
                    image_uuid = image_uuid[7:]
        n = image_uuid.rfind(":")
        if n < 0:
            return {'repo': image_uuid,
                    'tag': 'latest',
                    'uuid': image_uuid + ':latest'}
        tag = image_uuid[n+1:]
        if tag.find("/") < 0:
            return {'repo': image_uuid[:n], 'tag': tag, 'uuid': image_uuid}
        return {'repo': image_uuid,
                'tag': 'latest',
                'uuid': image_uuid + ':latest'}

    def volume_remove(self, req=None, volumeStoragePoolMap=None, **kw):
        volume = volumeStoragePoolMap.volume
        storage_pool = volumeStoragePoolMap.storagePool
        progress = Progress(req)

        with lock(volume):
            if volume.deviceNumber == 0:
                get_compute().purge_state(docker_client(), volume.instance)

            if not self._is_volume_removed(volume, storage_pool):
                self._do_volume_remove(volume, storage_pool, progress)

            data = self._get_response_data(req, volumeStoragePoolMap)
            return self._reply(req, data)


class ImageValidationError(Exception):
    pass


class AuthConfigurationError(Exception):
    pass
